"""Reusable answer bank for recurring application questions.

Stores confirmed question/answer pairs with their source and confirmation
status, so a later application doesn't re-ask something already answered.
Matching is deliberately conservative: exact text match only (case- and
whitespace-normalized). This module does NOT judge whether two differently
worded questions mean the same thing — that requires semantic judgment,
which is an LLM's job, not deterministic code's. Reusing an answer for a
differently worded question is a decision for whoever prepares the
application plan (a chat session, or a human), not something this module
does silently.

Only entries with confirmed=true are ever returned by find_exact_match —
a draft/unconfirmed entry is visible in `list` output but is never treated
as an approved answer.

Python 3.10+. Standard library only.
"""
import argparse
import json
from datetime import date
from pathlib import Path

DEFAULT_BANK_PATH = Path('private/answer_bank.json')


def normalize(question):
    return ' '.join(question.strip().lower().split())


def load_bank(path=DEFAULT_BANK_PATH):
    path = Path(path)
    if not path.exists():
        return {'entries': []}
    return json.loads(path.read_text(encoding='utf-8'))


def save_bank(bank, path=DEFAULT_BANK_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def find_exact_match(bank, question):
    """Returns the most recent CONFIRMED entry whose normalized question text
    matches exactly, or None. Never returns an unconfirmed draft."""
    target = normalize(question)
    matches = [e for e in bank['entries'] if e['confirmed'] and normalize(e['question']) == target]
    if not matches:
        return None
    return max(matches, key=lambda e: e['date'])


def add_answer(bank, question, answer, source, confirmed, day=None):
    if not question.strip() or not answer.strip():
        raise ValueError('question and answer must be non-empty')
    if not source.strip():
        raise ValueError('source is required — where this answer came from (e.g. "profile", '
                          '"user confirmed 2026-09-24", "posting for Acme BI Intern")')
    entry = {
        'question': question.strip(),
        'answer': answer.strip(),
        'source': source.strip(),
        'confirmed': bool(confirmed),
        'date': day or date.today().isoformat(),
    }
    bank.setdefault('entries', []).append(entry)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank', default=str(DEFAULT_BANK_PATH))
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('list')

    add_cmd = sub.add_parser('add')
    add_cmd.add_argument('question')
    add_cmd.add_argument('answer')
    add_cmd.add_argument('--source', required=True)
    add_cmd.add_argument('--confirmed', action='store_true',
                          help='Only a human-confirmed answer is ever reused automatically.')

    find_cmd = sub.add_parser('find')
    find_cmd.add_argument('question')

    args = parser.parse_args()
    bank = load_bank(args.bank)

    if args.command == 'list':
        print(json.dumps(bank['entries'], ensure_ascii=False, indent=2))
    elif args.command == 'add':
        entry = add_answer(bank, args.question, args.answer, args.source, args.confirmed)
        save_bank(bank, args.bank)
        print(json.dumps(entry, ensure_ascii=False, indent=2))
    elif args.command == 'find':
        match = find_exact_match(bank, args.question)
        print(json.dumps(match, ensure_ascii=False, indent=2) if match else 'null')


if __name__ == '__main__':
    main()
