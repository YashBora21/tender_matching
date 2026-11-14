"""Read-only cache recovery audit;keeps historical quarantine artifacts untouched.

Example: python eval/reassess_source_context.py --output-dir .audit_tmp/part-a-audit
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.services.cli.fact_cache import main

if __name__=='__main__':raise SystemExit(main())
