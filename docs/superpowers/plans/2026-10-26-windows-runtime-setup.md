# Windows 容器运行环境准备

日期：2026-10-26，Asia/Shanghai。此阶段承接用户在“下一步先让Docker运行环境就绪”后明确要求“继续下一步”。

**最新状态：用户已重启，Docker安装及真实联调已完成。** WSL3.0.3.0，Docker Desktop4.96.3、Engine29.9.2、Composev5.6.1；8个服务healthy。Redis/Chroma、默认embedding、Langfuse读回及3条真实smoke通过，完整证据见[真实联调结果](2026-10-26-live-integration-results.md)。下方保留安装前与首次失败历史。

## 授权与执行范围

- Ruling：将本次“继续下一步”视为继续准备并尝试安装前述Docker/WSL联调环境的授权；不重复询问是否开始。需要Windows管理员权限时通过正常UAC，由用户在系统弹窗确认。
- 不代替用户接受Docker服务协议；不自动重启Windows。不改变BIOS或现有业务服务配置。
- 安装包仅从Docker/Microsoft官方源取得；先验证来源、大小、摘要与Windows数字签名，再安装。
- 所有安装包、一次性安装脚本和运行日志放在被Git忽略的 `backend/data/integration/`。不将原项目凭据复制到脚本。
- 项目源代码保持现有工作分支，不提交、推送或部署。

## 安装前实测

| 项目 | 结果 |
| --- | --- |
| 系统 | Windows 11 专业版，build 26200，64位 |
| 硬件 | Lenovo本机，8逻辑处理器，15.8GiB内存 |
| 磁盘空闲 | 检查时C约76.1GiB，D约142.9GiB |
| HypervisorPresent | true；虚拟化/SLAT的WMI标志为false，已有hypervisor时不能仅据此判断BIOS关闭 |
| WSL | `wsl --status`退出50，明确提示未安装 |
| Docker | PATH及已知系统/用户安装目录均未发现 |
| 当前权限 | 非管理员；正常UAC授权后才能启用WSL |
| LanmanServer | Running / Automatic |
| 已有配置 | 未发现用户.wslconfig或backend/.env.integration |

## 顺序

1. 从官方更新源解析稳定版安装信息，下载到本地并核验。当前解析为Docker Desktop4.96.3、build244472（官方发布时间2026-10-22）。
2. 正常UAC启动一次性WSL安装脚本，使用 `wsl --install --no-distribution --web-download`，不额外安装Ubuntu。
3. 核验Docker安装包签名后，以per-user、WSL2方式安装；保留首次协议界面由用户操作。
4. 若系统需要重启，记录准确状态及重启后恢复命令。重启由用户安排。
5. Docker engine就绪后再执行Compose config与Redis/Chroma启动、真实存储检查，随后进入Langfuse。

此文档中的步骤不是通过证据。实际安装与服务状态在后续执行结果中逐项记录。

## 重启前首次执行结果（历史）

- 官方Docker安装包已下载，641758128字节；官方checksums.txt与本机SHA256一致：`3d8adad4f6c86583e0b00ea19a76197c51ccf5c83437c9ed775b739756452507`。Windows Authenticode为Valid，签名者Docker Inc。
- 已尝试per-user安装、WSL2后端及D盘虚拟磁盘目录，未使用accept-license参数。安装器退出1，明确报错：`VirtualMachinePlatform`未启用，per-user安装不能代为启用该系统功能。
- 已通过正常UAC发起WSL安装。未得到有效WSL安装记录，后一次Windows明确返回“操作已被用户取消”；因此不继续重复管理员提示，不声称WSL已安装。
- 未重启Windows，未接受Docker服务协议。当前Docker engine、Redis及Langfuse均未启动。
- 安装包：`backend/data/integration/installers/docker-desktop-4.96.3-244472.exe`；官方checksum和下载元数据在同目录。
- 本轮日志/状态：`backend/data/integration/setup/docker-install-failure.log`、`docker-install-result.json`、`windows-setup-status.json`，均被Git忽略。

## 恢复所需的用户系统操作

打开**管理员PowerShell**，执行：

```powershell
wsl --install --no-distribution --web-download
```

按Windows提示完成并自行重启。此步骤来自[微软WSL安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)，需要真实Windows管理员权限，不能由聊天确认替代系统授权。没有自动重启命令。

重启后先验证：

```powershell
wsl --version
wsl --status
```

随后可继续使用已缓存、已核验安装包，无需重新下载。以下命令仍不接受Docker服务协议：

```powershell
Set-Location D:\project\echomind\github-release\EchoMind
$dockerInstaller = (Resolve-Path backend/data/integration/installers/docker-desktop-4.96.3-244472.exe).Path
if ((Get-FileHash -LiteralPath $dockerInstaller -Algorithm SHA256).Hash -ne '3D8ADAD4F6C86583E0B00EA19A76197C51CCF5C83437C9ED775B739756452507') { throw 'Installer checksum mismatch' }
if ((Get-AuthenticodeSignature -LiteralPath $dockerInstaller).Status -ne 'Valid') { throw 'Installer signature invalid' }
Start-Process -FilePath $dockerInstaller -WindowStyle Hidden -Wait -ArgumentList 'install','--user','--quiet','--backend=wsl-2','--wsl-default-data-root=D:\project\echomind\.docker-wsl-data'
```

安装成功后首次打开Docker Desktop，由用户阅读并操作协议界面，再验证 `docker version` 的server部分；仅有client版本不能作为engine就绪证据。后续服务启动命令见[联调指南](../../integration.md)。

## 已完成的项目侧准备

- 生成被忽略的 `backend/.env.integration`，含9项随机本地服务密钥；未复制原模型凭据。已验证Git忽略状态，已有配置不覆盖。
- 准备并校验3条/3轮/17断言的独立smoke子数据集：问候、发票知识、跨会话安卓登录；保留原合成待审状态与完整知识快照，不改原120条数据。路径 `backend/data/integration/customer_service_smoke_v1.json`。
- 修复评测知识scope传递，新增6项回归；完整Python3.11依赖环境 **218 passed in16.43s**，JUnit errors/failures/skipped均0。见[回放选择与预算说明](2026-10-26-live-smoke-selection.md)。
- 新配置离线预检归档 `integration_b063ddbcdb91408982da60cd7259bd26.json`，0网络/模型请求，实时验收状态仍blocked。没有在依赖缺失时发起收费场景回放或提升baseline。

## 官方依据

- [Docker Windows安装及per-user/WSL要求](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Microsoft WSL安装](https://learn.microsoft.com/en-us/windows/wsl/install)
- [WSL no-distribution参数](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

## 重启前交接

- 用户提供的安装日志：WSL3.0.3安装完成，VirtualMachinePlatform启用100%成功，明确需要重启。
- 本机已验证版本，结果归档 `backend/data/integration/setup/wsl-reboot-required.json`。
- 尚未自动重启，Docker/Redis/Langfuse未启动。无需再次下载或执行wsl安装。
- 重启后顺序：检查WSL状态 → 使用缓存安装包安装Docker → 用户完成首次启动协议界面 → 检查engine → 启动独立Redis/Chroma → 存储检查 → 3条smoke及Langfuse验收。

## 重启后已执行

- 用户2026-10-26重启；WSL status退出0，默认版本2。Docker缓存包重新核验签名/摘要后per-user安装退出0，未使用accept-license参数。
- Docker原生Compose config通过，8个服务的真实启动/健康验证完成；未再请求管理员权限或重启。
- 固定版本存储、默认模型缓存、真实平台读回与3条smoke已通过。首轮1通过/2个Judge invalid及修复过程完整保留，没有伪造通过或覆盖报告。
- 最终后端回归257 passed in53.22s，无skip；服务保留运行。后续以真实联调结果为当前状态。
