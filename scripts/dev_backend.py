"""Start local development using the private dotenv files as the intended config."""
from pathlib import Path
import sys

from dotenv import load_dotenv
import uvicorn


def main():
    root = Path(__file__).resolve().parents[1]
    if not (root / '.env').is_file():
        raise SystemExit('Create the private root .env from .env.example before starting.')
    for path in (root / '.env', root / 'backend' / '.env'):
        load_dotenv(path, override=True)
    sys.path.insert(0, str(root / 'backend'))
    uvicorn.run('app.main:app', host='127.0.0.1', port=8000, reload=True,
                reload_dirs=[str(root / 'backend' / 'app')])


if __name__ == '__main__':
    main()
