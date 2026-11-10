"""Check exported image application files without printing configured private values."""
import argparse
import json
from pathlib import Path
import tempfile

from dotenv import dotenv_values

CONFIG_FILES = {
    'attribute_vocabulary.json', 'categories.json', 'doc_types.json',
    'doc_roles.json', 'normalization_policy.json',
}
PRIVATE_FIELDS = {
    'LLM_MODEL', 'LLM_BASE_URL', 'EMBEDDING_MODEL', 'EMBEDDING_BASE_URL',
    'OCR_VISION_MODEL', 'OCR_VISION_BASE_URL',
}
FORBIDDEN_DIRS = {'tests', 'raw', 'uploads', 'tenders', 'manufacturers', 'catalogue', '.git', '.audit_tmp', 'venv', '.venv'}


def configured_values(path):
    if not path.is_file():
        raise ValueError('private runtime configuration file is unavailable')
    values = {name.upper(): value for name, value in dotenv_values(path).items()
              if value and (name.upper().endswith('_API_KEY') or name.upper() in PRIVATE_FIELDS)}
    if not (any(name.endswith('_API_KEY') for name in values)
            and any(name.endswith('_MODEL') for name in values)
            and any(name.endswith('_BASE_URL') for name in values)):
        raise ValueError('private configuration must contain key, model and base-URL values')
    return set(values.values())


def check(roots, private_values, config_root=None):
    variants = set()
    for value in private_values:
        variants.update((value.encode('utf-8'), json.dumps(value, ensure_ascii=True)[1:-1].encode('utf-8'),
                         value.replace('/', '\\/').encode('utf-8')))
    issues, scanned = [], 0
    for root in roots:
        if not root.is_dir():
            issues.append({'root': root.name, 'reason': 'missing_image_export'})
            continue
        for path in root.rglob('*'):
            relative = path.relative_to(root)
            reason = None
            if path.is_symlink():
                reason = 'unexpected_symlink'
            elif any(part.lower() in FORBIDDEN_DIRS for part in relative.parts[:-1]):
                reason = 'unexpected_corpus_or_test_artifact'
            elif path.is_file():
                scanned += 1
                if path.name == '.env' or path.name.startswith('.env.'):
                    reason = 'embedded_environment_file'
                elif any(pattern in path.read_bytes() for pattern in variants):
                    reason = 'configured_private_value_present'
            if reason:
                label = relative.as_posix()
                for private in sorted(private_values, key=len, reverse=True):
                    label = label.replace(private, '[redacted]')
                issues.append({'root': root.name, 'path': label, 'reason': reason})
    if config_root is not None:
        files = {path.name for path in config_root.iterdir()} if config_root.is_dir() else set()
        if files != CONFIG_FILES:
            issues.append({'root': config_root.name, 'reason': 'configuration_allowlist_mismatch'})
        else:
            for filename in CONFIG_FILES:
                json.loads((config_root/filename).read_text(encoding='utf-8'))
    return {'passed': not issues, 'scanned_files': scanned,
            'configured_values_checked': len(private_values), 'issues': issues}


def self_test():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = root/'image-app';app.mkdir()
        private = {'synthetic-private-key-abc', 'synthetic-private-model-id', 'synthetic/private/provider'}
        (app/'clean.py').write_text('synthetic safe code', encoding='utf-8')
        assert check([app], private)['passed']
        for value in private:
            (app/'bad.py').write_text(json.dumps(value), encoding='utf-8')
            result = check([app], private)
            assert not result['passed'] and all(value not in json.dumps(result) for value in private)
        (app/'bad.py').unlink()
        (app/'.env').write_text('synthetic fixture', encoding='utf-8')
        assert not check([app], private)['passed']
        (app/'.env').unlink()
        (root/'historical-source-doc.md').write_text(next(iter(private)), encoding='utf-8')
        assert check([app], private)['passed']
        config = root/'image-config';config.mkdir()
        for filename in CONFIG_FILES:
            (config/filename).write_text('{}', encoding='utf-8')
        assert check([app, config], private, config)['passed']
        (config/'extra.json').write_text('{}', encoding='utf-8')
        assert not check([app, config], private, config)['passed']
    print(json.dumps({'self_test': 'passed'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-env', type=Path)
    parser.add_argument('--root', type=Path, action='append')
    parser.add_argument('--config-root', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test();return 0
    if not args.runtime_env or not args.root:
        parser.error('--runtime-env and at least one --root are required')
    try:
        result = check(args.root, configured_values(args.runtime_env), args.config_root)
    except Exception as exc:
        print(json.dumps({'passed': False, 'error_type': type(exc).__name__}));return 2
    print(json.dumps(result));return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
