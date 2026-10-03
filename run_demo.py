import json
from pathlib import Path
import radar

def main():
    state=radar.empty()
    for path in sorted(Path('demo/postings').glob('*.json')):
        k=radar.ingest(state,radar.read(path),'2026-09-09')['key']
        result=radar.assess(state,k,radar.read(Path('demo/assessments')/path.name),'2026-09-09')
        print(path.stem, state['jobs'][k]['posting']['role'], result['decision'])
    print(json.dumps(radar.shortlist(state,'2026-09-09'),indent=2))
    radar.write('demo/state.json',state)
    radar.export(state,'demo/jobs.csv','2026-09-09')
    print('Fictional replay only. No model or live job search was run.')
if __name__=='__main__': main()
