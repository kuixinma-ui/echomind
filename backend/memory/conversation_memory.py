"""
亮点：多轮对话记忆管理

三级记忆架构，模拟人类记忆机制：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（ChromaDB）—— 跨会话的历史对话，按语义相似度检索
  3. 用户画像（ChromaDB）—— 从对话中提炼的长期偏好和实体

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - 所有 Embedding 通过 Anthropic API 生成，无本地模型
"""
import hashlib
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from anthropic import AsyncAnthropic
from redis.exceptions import WatchError

from core.llm_utils import extract_text_content, parse_structured_response, structured_request_options
from context import ContextRef, profile_diff
from memory.roles import MsgRole
from observability.budgets import EvaluationBudgetExceeded
from services.storage import (collection_name, create_chroma_client, memory_key,
                              identity_text, require_evaluation_namespace, validate_namespace)

logger = logging.getLogger(__name__)


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    user_profile:     Dict[str, Any]  # 用户画像：偏好、常用实体
    summary:          str             # 当前会话摘要（压缩后）
    handoff_user_messages: List[Dict[str, str]] = field(default_factory=list)

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[用户画像]\n以下画像可能包含模型推断，需结合用户更正确认，不代表已核实业务事实。\n{json.dumps(self.user_profile, ensure_ascii=True)}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages[-8:]:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和用户画像存 ChromaDB（持久化）。
    """

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    SUMMARY_MAX_CHARS = 800
    PROFILE_DOC_PREFIX = "user_profile:"
    HANDOFF_METADATA_KEY = "handoff_user_messages_v1"
    HANDOFF_MAX_MESSAGES = 20
    HANDOFF_MAX_CONTENT = 1000
    HANDOFF_MAX_JSON = 32768

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
        namespace: str = "",
        chroma_mode: str = 'auto',
        redis_client=None,
        chroma_client=None,
        client=None,
    ):
        self.namespace = validate_namespace(namespace)
        # The async runtime can also unwind this instance if __init__ fails.
        self._runtime_initialized = False
        self._runtime_collection_names = []
        self._client = client
        self._redis = redis_client
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._model  = model

        # ChromaDB：优先连接独立服务（docker compose 模式），连不上则降级为本地嵌入式
        chroma = chroma_client if chroma_client is not None else create_chroma_client(chroma_host, chroma_port, chroma_path, mode=chroma_mode)
        self._chroma = chroma
        try:
            for base, attribute in (("episodic", "_episodic"), ("user_profile", "_profile")):
                name = collection_name(namespace, base)
                setattr(self, attribute, chroma.get_or_create_collection(name))
                self._runtime_collection_names.append(name)
            # Allocate async clients only after both synchronous collections exist.
            self._redis = redis_client if redis_client is not None else redis.from_url(redis_url, decode_responses=True)
            self._client = client if client is not None else AsyncAnthropic(**kwargs)
        except BaseException:
            if namespace:
                try:
                    require_evaluation_namespace(namespace)
                except ValueError:
                    pass
                else:
                    for name in tuple(self._runtime_collection_names):
                        try:
                            chroma.delete_collection(name)
                        except Exception:
                            # Leave failed deletions owned by the async runtime.
                            continue
                        self._runtime_collection_names.remove(name)
            raise
        self._profile_locks: Dict[str, asyncio.Lock] = {}
        self._runtime_initialized = True

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """将一条消息写入工作记忆，超阈值时自动压缩。"""
        user_id = identity_text(user_id)
        conv_id = identity_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        # 追加到 Redis 列表（左推，最新在前）
        await self._redis.lpush(key, json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        }))
        await self._redis.expire(key, 86400)  # 24h TTL

        # 超过压缩阈值时触发压缩
        if await self._redis.llen(key) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def archive_session(self, user_id: str, conv_id: str) -> Dict[str, Any]:
        """Make a completed session recallable across sessions, without LLM inference.

        Explicit archival must fail on storage errors so an evaluation cannot
        silently proceed with missing initial facts. Repeating the same archive
        upserts the same record; the working conversation is left intact.
        """
        from context.memory_evidence import memory_evidence_id
        user_id, conv_id = identity_text(user_id), identity_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id, complete=True)
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ''
        text = '\n'.join(([f'[earlier summary] {summary}'] if summary else []) +
                         [f'{message.role.value}: {message.content}' for message in messages]).strip()
        if not text:
            raise ValueError('Cannot archive an empty session')
        evidence_id = memory_evidence_id(text)
        document_id = hashlib.sha256(json.dumps([user_id, conv_id, evidence_id]).encode()).hexdigest()
        await asyncio.to_thread(self._episodic.upsert, ids=[document_id], documents=[text], metadatas=[{
            'user_id': user_id, 'conv_id': conv_id, 'source': 'session_archive',
            'evidence_id': evidence_id, 'ts': datetime.now().isoformat(), 'summary_inferred': bool(summary),
            self.HANDOFF_METADATA_KEY: self._serialize_handoff_users(messages),
        }])
        return {'evidence_id': evidence_id, 'source_conv': conv_id, 'message_count': len(messages)}

    async def update_profile(self, user_id: str, conv_id: str) -> bool:
        """Return False on extraction/storage failure; an empty conversation is a no-op success."""
        user_id, conv_id = identity_text(user_id), identity_text(conv_id)
        async with self._profile_locks.setdefault(user_id, asyncio.Lock()):
            return await self._update_profile(user_id, conv_id)

    async def _update_profile(self, user_id: str, conv_id: str) -> bool:
        """
        从当前工作记忆中提炼用户偏好，更新用户画像。
        用 LLM 提炼偏好，然后存入 ChromaDB（ChromaDB 内置 embedding，不依赖外部 API）。
        """
        user_id = identity_text(user_id)
        conv_id = identity_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return True

        current_profile = await self._get_profile(user_id)

        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        profile_ctx = json.dumps(current_profile, ensure_ascii=False) if current_profile else "{}"
        prompt = f"""从以下对话和已有用户画像中提炼或更新用户偏好和关键实体，返回 JSON。
对话:
{text}

已有画像:
{profile_ctx}

明确更正应替换旧值；明确要求忘记的信息应删除对应字段。以下结果只是模型推断，不能视为已核实业务事实。
返回格式: {{"preferences": ["..."], "entities": {{"产品": [], "问题类型": []}}}}"""
        prompt = self._safe_text(prompt)

        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=512, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
                **structured_request_options(),
            )
            profile_data = parse_structured_response(resp, dict)
            if not isinstance(profile_data, dict):
                raise ValueError('Profile extraction must return an object')
            await self._write_profile(user_id, conv_id, profile_data, current_profile, inferred=True)
            logger.info(f"用户画像已更新: {user_id}")
            return True
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")
            return False

    async def seed_profile(self, user_id: str, profile: Dict[str, Any], *,
                           source_conv: str = 'scenario_initial_state') -> Dict[str, Any]:
        """Experiment-only fixture seeding, never available for production namespaces."""
        require_evaluation_namespace(self.namespace)
        if not isinstance(profile, dict):
            raise ValueError('Profile seed must be an object')
        user_id, source_conv = identity_text(user_id), identity_text(source_conv)
        async with self._profile_locks.setdefault(user_id, asyncio.Lock()):
            before = await self._get_profile(user_id)
            return await self._write_profile(user_id, source_conv, profile,
                                             before, inferred=False, experiment_seed=True)

    async def _write_profile(self, user_id, conv_id, profile, before, *, inferred, experiment_seed=False):
        user_id, conv_id = identity_text(user_id), identity_text(conv_id)
        doc_id = self._profile_doc_id(user_id)
        stored = await asyncio.to_thread(self._profile.get, ids=[doc_id])
        metadata = (stored.get('metadatas') or [{}])[0] or {}
        version = str(int(metadata.get('version', '0')) + 1)
        updated_at = datetime.now().isoformat()
        ref = ContextRef(id=doc_id, type='user_profile', version=version,
                         source=conv_id, scope='user:' + user_id, effective_at=updated_at)
        audit = {**profile_diff(before, profile), 'source_conv': conv_id,
                 'version': version, 'inferred': inferred, 'experiment_seed': experiment_seed,
                 'context_ref': ref.to_dict(), 'updated_at': updated_at}
        # One upsert keeps the old profile readable until the new document replaces it.
        await asyncio.to_thread(self._profile.upsert, ids=[doc_id],
                                documents=[self._safe_text(json.dumps(profile, ensure_ascii=False))],
                                metadatas=[{'user_id': user_id, 'conv_id': conv_id,
                                            'source_conv': conv_id, 'updated_at': updated_at,
                                            'inferred': inferred, 'experiment_seed': experiment_seed,
                                            **ref.to_metadata()}])
        await self._redis.lpush(self._profile_audit_key(user_id), json.dumps(audit, ensure_ascii=True))
        return audit

    async def get_profile_audit(self, user_id: str) -> List[Dict[str, Any]]:
        """Read this user's last 10,000 profile changes in chronological order."""
        rows = await self._redis.lrange(self._profile_audit_key(identity_text(user_id)), 0, 9999)
        return [json.loads(row) for row in reversed(rows)]

    def _profile_audit_key(self, user_id: str) -> str:
        return memory_key(self.namespace, 'profile_audit', user_id, '')

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 1. 工作记忆（当前会话最近消息）
        user_id = identity_text(user_id)
        conv_id = identity_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history, handoff_users = await self._search_episodic_context(
            user_id,
            conv_id,
            query or (recent[-1].content if recent else ""),
        )

        # 3. 用户画像
        profile = await self._get_profile(user_id)

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
            handoff_user_messages=handoff_users,
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> bool:
        """
        工作记忆压缩：
          1. 用 LLM 对旧消息生成摘要
          2. 确认最早一批消息已归档，再发布摘要和裁剪工作列表
          3. 同一 Redis WATCH/MULTI 提交，快照变化则放弃本次提交

        Chroma/Redis 不构成共同事务；Redis 提交失败可能留下已确认的归档，
        稳定批次身份允许重试。每次只处理有界前缀，积压的其余消息完整保留。
        """
        user_id, conv_id = identity_text(user_id), identity_text(conv_id)
        key = self._wm_key(user_id, conv_id)
        skey = self._summary_key(user_id, conv_id)
        # Context reads remain bounded; compaction must see the unarchived prefix
        # even after repeated failures grow this list beyond WORKING_MAX.
        snapshot = list(await self._redis.lrange(key, 0, -1))
        messages = self._working_messages(snapshot)
        if len(messages) < self.COMPRESS_AT:
            return False

        batch_size = self.COMPRESS_AT - 5
        if batch_size <= 0:
            raise ValueError('Compression threshold must exceed five retained messages')
        to_compress = messages[:batch_size]
        # Keep None distinct from an explicitly stored empty summary for WATCH.
        old_summary = await self._redis.get(skey)

        # LLM 摘要
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"用 2-3 句话总结以下对话的关键信息：\n{text}")
        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(extract_text_content(resp.content)).strip()
        except (EvaluationBudgetExceeded, asyncio.CancelledError):
            raise
        except Exception:
            summary = f"对话包含 {len(to_compress)} 条消息（摘要生成失败）"

        new_summary = await self._merge_summary(old_summary or "", summary)

        # A failed or cancelled archive cannot justify replacing the old summary
        # or removing its source messages. A cancelled thread may still finish;
        # its stable upsert identity makes a later retry reuse this batch.
        await self._store_episodic(user_id, conv_id, text, summary, user_messages=to_compress)

        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key, skey)
                if (await pipe.lrange(key, 0, -1) != snapshot
                        or await pipe.get(skey) != old_summary):
                    logger.info('工作记忆压缩暂缓：源快照已变化')
                    return False
                pipe.multi()
                pipe.setex(skey, 86400, new_summary)
                # Redis lists are newest first. Only remove the archived oldest
                # prefix; do not reconstruct or drop a newer unarchived backlog.
                pipe.ltrim(key, 0, len(snapshot) - len(to_compress) - 1)
                pipe.expire(key, 86400)
                await pipe.execute()
        except WatchError:
            logger.info('工作记忆压缩暂缓：提交期间源快照已变化')
            return False
        logger.info(f"工作记忆压缩完成: {user_id}/{conv_id}，摘要 {len(summary)} 字")
        return True

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str, *, complete: bool = False) -> List[Message]:
        key  = self._wm_key(user_id, conv_id)
        raws = await self._redis.lrange(key, 0, -1 if complete else self.WORKING_MAX - 1)
        return self._working_messages(raws)

    @staticmethod
    def _working_messages(raws) -> List[Message]:
        msgs = []
        for raw in reversed(raws):  # Redis lpush 最新在前，reversed 还原时序
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, conv_id: str, query: str) -> List[str]:
        """语义检索情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        history, _ = await self._search_episodic_context(user_id, conv_id, query)
        return history

    async def _search_episodic_context(self, user_id: str, conv_id: str, query: str):
        """Use the same selected documents for recall and typed handoff provenance."""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return [], []
        try:
            results = await self._query_episodic(
                query_text,
                n_results=self.HISTORY_TOP_K,
                where={"$and": [{"user_id": identity_text(user_id)}, {"conv_id": identity_text(conv_id)}]},
            )
            rows = self._episodic_rows(results)
            if len(rows) < self.HISTORY_TOP_K:
                fallback = await self._query_episodic(
                    query_text,
                    n_results=self.HISTORY_TOP_K,
                    where={"user_id": identity_text(user_id)},
                )
                rows.extend(self._episodic_rows(fallback))
            history, handoff_users, seen = [], [], set()
            for document, metadata in rows:
                text = document.strip()
                if text in seen:
                    continue
                seen.add(text)
                history.append(text)
                handoff_users.extend(self._read_handoff_users(metadata, user_id, text))
                if len(history) >= self.HISTORY_TOP_K:
                    break
            return history, handoff_users
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return [], []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str,
                              *, user_messages: Optional[List[Message]] = None) -> None:
        """Confirm an idempotent archive upsert; storage failures propagate."""
        user_id = identity_text(user_id)
        conv_id = identity_text(conv_id)
        text = self._safe_text(text)
        summary = self._safe_text(summary)
        identity = [user_id, conv_id, text, [
            [message.role.value, message.timestamp.isoformat()]
            for message in user_messages or [] if isinstance(message, Message)
        ]]
        doc_id = 'compression_' + hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
        # The generated summary is intentionally not part of batch identity:
        # retries can produce different summaries for the same original batch.
        await asyncio.to_thread(
            self._episodic.upsert,
            ids=[doc_id],
            documents=[summary],
            metadatas=[{"user_id": user_id, "conv_id": conv_id,
                        "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500]),
                        "source": "conversation_compression",
                        self.HANDOFF_METADATA_KEY: self._serialize_handoff_users(user_messages or [])}],
        )

    @classmethod
    def _serialize_handoff_users(cls, messages: List[Message]) -> str:
        # Message.metadata and rendered text never establish a role.
        records = [{'role': 'user', 'content': cls._safe_text(message.content)[:cls.HANDOFF_MAX_CONTENT]}
                   for message in messages if isinstance(message, Message) and message.role == MsgRole.USER]
        records = records[-cls.HANDOFF_MAX_MESSAGES:]
        encoded = json.dumps(records, ensure_ascii=False)
        while len(encoded) > cls.HANDOFF_MAX_JSON and records:
            records.pop(0)
            encoded = json.dumps(records, ensure_ascii=False)
        return encoded

    @staticmethod
    def _episodic_rows(results: Dict[str, Any]):
        documents = results.get('documents') or []
        metadatas = results.get('metadatas') or []
        documents = documents[0] if documents and isinstance(documents[0], list) else documents
        metadatas = metadatas[0] if metadatas and isinstance(metadatas[0], list) else metadatas
        return [(document, metadatas[index] if index < len(metadatas) else None)
                for index, document in enumerate(documents)
                if isinstance(document, str) and document.strip()]

    @classmethod
    def _read_handoff_users(cls, metadata, user_id, document):
        from context.memory_evidence import memory_evidence_id
        if not isinstance(metadata, dict) or metadata.get('user_id') != identity_text(user_id):
            return []
        if metadata.get('source') not in {'session_archive', 'conversation_compression'}:
            return []
        source_conv = metadata.get('conv_id')
        if not isinstance(source_conv, str) or not source_conv.strip() or len(source_conv) > 256:
            return []
        evidence_id = memory_evidence_id(document)
        if metadata.get('evidence_id', evidence_id) != evidence_id:
            return []
        encoded = metadata.get(cls.HANDOFF_METADATA_KEY)
        if not isinstance(encoded, str) or len(encoded) > cls.HANDOFF_MAX_JSON:
            return []
        try:
            records = json.loads(encoded)
        except (ValueError, RecursionError):
            return []
        if not isinstance(records, list) or len(records) > cls.HANDOFF_MAX_MESSAGES:
            return []
        if any(not isinstance(record, dict) or set(record) != {'role', 'content'}
               or record.get('role') != 'user'
               or not isinstance(record.get('content'), str)
               or len(record['content']) > cls.HANDOFF_MAX_CONTENT for record in records):
            return []
        return [{'role': 'user', 'content': record['content'], 'source_conv': source_conv,
                 'evidence_id': evidence_id} for record in records if record['content'].strip()]

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像（取最新一条）。"""
        try:
            doc_id = self._profile_doc_id(user_id)
            direct = await asyncio.to_thread(self._profile.get, ids=[doc_id])
            if direct.get("documents"):
                return json.loads(direct["documents"][0])

            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id})
            return self._latest_profile_from_results(results)
        except Exception:
            pass
        return {}

    async def close(self) -> None:
        """关闭异步 Redis 连接。"""
        await self._redis.aclose()
        await self._client.close()

    async def cleanup(self) -> None:
        require_evaluation_namespace(self.namespace)
        async for key in self._redis.scan_iter(match=f"{self.namespace}:*"):
            await self._redis.delete(key)
        for base in ("episodic", "user_profile"):
            await asyncio.to_thread(self._chroma.delete_collection, collection_name(self.namespace, base))

    def _wm_key(self, user_id: str, conv_id: str) -> str:
        return memory_key(self.namespace, "wm", user_id, conv_id)

    def _summary_key(self, user_id: str, conv_id: str) -> str:
        return memory_key(self.namespace, "summary", user_id, conv_id)

    @classmethod
    def _profile_doc_id(cls, user_id: str) -> str:
        return f"{cls.PROFILE_DOC_PREFIX}{user_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """转成 ChromaDB 可接受的普通 UTF-8 字符串。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """递归清洗 metadata，避免 Redis/ChromaDB 后续读写遇到非法 UTF-8。"""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value

    async def _query_episodic(
        self,
        query_text: str,
        n_results: int,
        where: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._episodic.query,
            query_texts=[query_text],
            n_results=n_results,
            where=where,
        )

    @staticmethod
    def _extract_docs(results: Dict[str, Any]) -> List[str]:
        docs = results.get("documents") or []
        if not docs:
            return []
        first = docs[0] if isinstance(docs[0], list) else docs
        return [doc for doc in first if isinstance(doc, str) and doc.strip()]

    @staticmethod
    def _dedupe_texts(values: List[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for value in values:
            text = value.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            deduped.append(text)
        return deduped

    async def _merge_summary(self, old_summary: str, new_summary: str) -> str:
        old_summary = self._safe_text(old_summary).strip()
        new_summary = self._safe_text(new_summary).strip()
        if not old_summary:
            return new_summary[: self.SUMMARY_MAX_CHARS]
        if not new_summary:
            return old_summary[: self.SUMMARY_MAX_CHARS]

        prompt = self._safe_text(
            f"""你是对话摘要器。请把下面两段摘要合并为一段不超过 {self.SUMMARY_MAX_CHARS} 个中文字符的摘要。
保留：用户偏好、关键实体、待办事项、约束条件、未解决问题。
只输出摘要正文，不要编号，不要解释。

旧摘要:
{old_summary}

新增摘要:
{new_summary}
"""
        )
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            merged = self._safe_text(extract_text_content(resp.content)).strip()
            if merged:
                return merged[: self.SUMMARY_MAX_CHARS]
        except (EvaluationBudgetExceeded, asyncio.CancelledError):
            raise
        except Exception as ex:
            logger.warning(f"合并摘要失败，回退为截断拼接: {ex}")

        merged = self._safe_text(f"{old_summary}\n{new_summary}").strip()
        return merged[-self.SUMMARY_MAX_CHARS :]

    @staticmethod
    def _latest_profile_from_results(results: Dict[str, Any]) -> Dict[str, Any]:
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        if not documents:
            return {}

        candidates: List[tuple[str, Dict[str, Any], str]] = []
        for idx, doc in enumerate(documents):
            if not doc:
                continue
            metadata = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
            ts = str(metadata.get("updated_at") or metadata.get("ts") or "")
            candidates.append((ts, metadata, doc))

        if not candidates:
            return {}

        candidates.sort(key=lambda item: item[0], reverse=True)
        latest_doc = candidates[0][2]
        try:
            return json.loads(latest_doc)
        except Exception:
            return {}
