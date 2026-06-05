# Alpha53 testing

Alpha53 tests should be run after alpha52.1 has produced:

```text
alpha52_1_provider_result_verified.json
```

Expected alpha53 outputs:

```text
alpha53_positive_policy_validation.json
alpha53_negative_tampered_digest.json
alpha53_negative_audit_failure.json
alpha53_trust_demo_summary.json
```

Expected verdict:

```text
positive policy validation: ok=true
negative tampered digest: ok=false
negative audit failure: ok=false
summary overall_ok=true
```
