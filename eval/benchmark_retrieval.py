"""Read-only fixed-tender retrieval benchmark; no generative model calls.

Run from backend: ../venv/Scripts/python.exe ../eval/benchmark_retrieval.py
The first two technical requirements are a bounded smoke benchmark, not an SLA.
"""
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text
from sqlalchemy.orm import Session
from app.repositories.db import get_engine
from app.services.match import retrieve
from app.services.match.match import build_query
from app.services.llm import _sentence_transformer
from app.core.config import get_settings


def main():
    settings = get_settings()
    started = time.perf_counter()
    model = _sentence_transformer(settings.embedding_model)
    print(json.dumps({"device": str(model.device), "model_load_seconds": time.perf_counter()-started}), flush=True)
    with Session(get_engine()) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        rows = session.execute(text("""
            select r.id,r.text,r.attribute,r.value,r.unit
            from requirement r join tender t on t.id=r.tender_id
            where t.id::text like '2bc8f917%' and r.type='technical'
            order by r.id limit 2
        """)).mappings().all()
        if len(rows) != 2:
            raise RuntimeError("fixed benchmark tender needs two technical requirements")
        original = retrieve.embed
        results = []
        for reuse in (False, True, False, True):
            calls = []
            def counted(texts):
                calls.append(len(texts))
                return original(texts)
            retrieve.embed = counted
            session.info.clear()
            if reuse:
                session.info["requirement_embeddings"] = {}
            started = time.perf_counter()
            outputs = []
            try:
                for name in ("Flowserve", "delval_flow", "valmatic"):
                    for row in rows:
                        query = build_query(requirement_text=row['text'], attribute=row['attribute'], value=row['value'], unit=row['unit'])
                        candidates = retrieve.search(session, query, manufacturer_name=name, category="valves", limit=3)
                        outputs.append([str(row['id']),name,[c.chunk_id for c in candidates]])
                results.append({"reuse":reuse,"seconds":round(time.perf_counter()-started,3),"embedding_calls":len(calls),"pairs":outputs})
            finally:
                retrieve.embed = original
        session.rollback()
    print(json.dumps({"results":[{k:v for k,v in r.items() if k != 'pairs'} for r in results],"candidate_equality":all(r['pairs']==results[0]['pairs'] for r in results),"requirement_ids":[str(r['id']) for r in rows]},default=str),flush=True)


if __name__ == '__main__':
    main()
