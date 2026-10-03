"""Cover-letter draft, approval, and document generation.

Drafting the actual sentences is still an LLM's job — a chat session (or a
future llm_client.py extension) writes tailored text using only verified
facts from the profile and the posting, the same division of labor as
radar.py's assessments: the model judges/writes, deterministic code stores,
gates, and verifies.

This module never invents content. It stores whatever text it is given,
requires an explicit human `approve` step before `generate` will run (the
same approval gate apply.py uses for its fill plan), and after generating
the .docx it reads the file back and checks the extracted text matches the
approved text exactly — the same read-back-and-verify principle apply.py
uses for form fields, applied to a generated document instead of a page.

Python 3.10+. Requires `python-docx` for `generate` only; `draft`/`approve`/
`list` need no extra dependency.
"""
import argparse
import json
from datetime import date
from pathlib import Path

DOCS_DIR = Path('private/documents/cover_letters')


def record_path(key):
    return DOCS_DIR / (key + '.json')


def load_record(key):
    path = record_path(key)
    if not path.exists():
        raise ValueError('No cover letter draft for key: ' + key)
    return json.loads(path.read_text(encoding='utf-8'))


def save_record(key, record):
    path = record_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def draft(key, company, role, text, source_notes, day=None):
    if not text.strip():
        raise ValueError('Cover letter text must not be empty')
    record = {
        'key': key,
        'company': company,
        'role': role,
        'text': text.strip(),
        'source_notes': source_notes,
        'approved': False,
        'approved_on': None,
        'generated_docx_path': None,
        'generated_verified': False,
        'drafted_on': day or date.today().isoformat(),
    }
    save_record(key, record)
    return record


def approve(key, day=None):
    record = load_record(key)
    record['approved'] = True
    record['approved_on'] = day or date.today().isoformat()
    save_record(key, record)
    return record


def generate(key):
    record = load_record(key)
    if not record['approved']:
        raise ValueError('Cover letter is not approved. Run `approve` after human review first.')
    import docx  # python-docx; only imported here so draft/approve need no extra dependency
    out_path = DOCS_DIR / (key + '.docx')
    document = docx.Document()
    for paragraph in record['text'].split('\n\n'):
        document.add_paragraph(paragraph.strip())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(out_path))

    # Read back and verify — a library bug, an encoding issue, or a future
    # change to this function should never silently ship a wrong document.
    reread = docx.Document(str(out_path))
    extracted = '\n\n'.join(p.text.strip() for p in reread.paragraphs if p.text.strip())
    expected = '\n\n'.join(p.strip() for p in record['text'].split('\n\n') if p.strip())
    verified = extracted == expected

    record['generated_docx_path'] = str(out_path)
    record['generated_verified'] = verified
    save_record(key, record)
    if not verified:
        raise RuntimeError('Generated document text does not match the approved text on read-back. '
                            'File written to %s but flagged unverified — do not upload it.' % out_path)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    draft_cmd = sub.add_parser('draft')
    draft_cmd.add_argument('key')
    draft_cmd.add_argument('--company', required=True)
    draft_cmd.add_argument('--role', required=True)
    draft_cmd.add_argument('--text-file', required=True, help='Path to a text file with the drafted letter.')
    draft_cmd.add_argument('--source-notes', required=True,
                            help='What facts this draft is based on (profile fields, posting text, etc.).')

    approve_cmd = sub.add_parser('approve')
    approve_cmd.add_argument('key')

    generate_cmd = sub.add_parser('generate')
    generate_cmd.add_argument('key')

    show_cmd = sub.add_parser('show')
    show_cmd.add_argument('key')

    args = parser.parse_args()
    if args.command == 'draft':
        text = Path(args.text_file).read_text(encoding='utf-8')
        result = draft(args.key, args.company, args.role, text, args.source_notes)
    elif args.command == 'approve':
        result = approve(args.key)
    elif args.command == 'generate':
        result = generate(args.key)
    elif args.command == 'show':
        result = load_record(args.key)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
