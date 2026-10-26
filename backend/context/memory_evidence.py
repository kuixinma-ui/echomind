"""Content identifiers for recalled memory, without returning memory text to UI."""
import hashlib


def memory_evidence_id(text):
    return 'mem_' + hashlib.sha256(text.strip().encode('utf-8')).hexdigest()
