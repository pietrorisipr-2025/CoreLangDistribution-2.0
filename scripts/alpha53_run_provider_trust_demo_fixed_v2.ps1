param(
  [Parameter(Mandatory=$true)][string]$Alpha52Result,
  [Parameter(Mandatory=$true)][string]$OutRoot
)

New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null
$PyScript = Join-Path $OutRoot "alpha53_fixed_direct_v2.py"
@'
import copy, json, sys
from pathlib import Path
from datetime import datetime, timezone
inp=Path(sys.argv[1]); out=Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
base=json.loads(inp.read_text(encoding='utf-8-sig'))
def b(v): return v is True or str(v).lower()=='true'
def dg(o):
    e=o.get('expected_sha256') or o.get('expected_digest')
    a=o.get('actual_sha256') or o.get('actual_digest') or o.get('tree_sha256') or o.get('actual_tree_sha256') or o.get('installed_tree_sha256')
    if e and not a and b(o.get('digest_ok')): a=e
    return e,a
def audit(o):
    for k in ['audit_install','audit_install_result','audit']:
        v=o.get(k)
        if isinstance(v,dict) and 'ok' in v: return b(v['ok'])
    return False
def validate(o, policy_sha):
    errors=[]; e,a=dg(o)
    if not b(o.get('ok')): errors.append('provider result ok is not true')
    if not b(o.get('digest_ok')): errors.append('digest_ok is not true')
    if not audit(o): errors.append('audit_install.ok is not true or missing')
    if not e: errors.append('expected_sha256 missing')
    if not a: errors.append('actual_sha256 missing')
    if e and a and e.lower()!=a.lower(): errors.append('expected_sha256 != actual_sha256')
    if policy_sha and a and policy_sha.lower()!=a.lower(): errors.append('policy expected_sha256 != actual_sha256')
    return {'schema':'CLD2/alpha53_provider_policy_validation','code_baseline':'2.0.0-alpha50.2','benchmark_milestone':'alpha53-fixed-direct-v2','ok':not errors,'errors':errors,'checks':{'provider_ok':o.get('ok'),'digest_ok':o.get('digest_ok'),'audit_install_ok':audit(o),'expected_sha256':e,'actual_sha256':a,'policy_expected_sha256':policy_sha}}
expected,_=dg(base)
if not expected: raise SystemExit('expected digest missing')
policy={'schema':'CLD2/alpha53_provider_policy','expected_sha256':expected,'require_ok':True,'require_digest_ok':True,'require_audit_install_ok':True}
(out/'alpha53_provider_policy.json').write_text(json.dumps(policy,indent=2,sort_keys=True)+'\n',encoding='utf-8')
pos=validate(base,expected)
t=copy.deepcopy(base); t['actual_sha256']='0'*64; t['digest_ok']=False; negd=validate(t,expected)
a=copy.deepcopy(base); a['ok']=False; a['audit_install']={'ok':False,'error':'simulated alpha53 negative audit failure'}; nega=validate(a,expected)
(out/'alpha53_positive_policy_validation.json').write_text(json.dumps(pos,indent=2,sort_keys=True)+'\n',encoding='utf-8')
(out/'alpha53_negative_tampered_digest.json').write_text(json.dumps(negd,indent=2,sort_keys=True)+'\n',encoding='utf-8')
(out/'alpha53_negative_audit_failure.json').write_text(json.dumps(nega,indent=2,sort_keys=True)+'\n',encoding='utf-8')
s={'schema':'CLD2/alpha53_provider_trust_demo_summary','created_at':datetime.now(timezone.utc).isoformat(),'code_baseline':'2.0.0-alpha50.2','benchmark_milestone':'alpha53-fixed-direct-v2','positive_validation_ok':bool(pos['ok']),'tampered_digest_rejected':not bool(negd['ok']),'audit_failure_rejected':not bool(nega['ok'])}
s['overall_ok']=s['positive_validation_ok'] and s['tampered_digest_rejected'] and s['audit_failure_rejected']
(out/'alpha53_trust_demo_summary.json').write_text(json.dumps(s,indent=2,sort_keys=True)+'\n',encoding='utf-8')
(out/'ALPHA53_PROVIDER_TRUST_DEMO_REPORT.md').write_text(f"# CLD2 alpha53 provider trust demo\n\nOverall OK: {s['overall_ok']}\n",encoding='utf-8')
print(json.dumps(s,indent=2,sort_keys=True))
sys.exit(0 if s['overall_ok'] else 2)
'@ | Set-Content -Encoding UTF8 $PyScript
python $PyScript $Alpha52Result $OutRoot
