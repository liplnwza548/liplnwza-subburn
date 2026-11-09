# Next-phase proposals (not implemented)

1. Authorization policy and threat model: define who authorizes auto-produce-to-review, output restrictions, revocation and audit trail; acceptance includes missing/invalid authorization and no-publish tests. Preserve current gate until replacement is designed and approved.
2. One-clip portable vertical slice: shared VM/Colab orchestration with capability checks, explicit style, synthetic offline integration, same source protection and evidence-based QC; depends on authorization policy.
3. Style eligibility: isolate generic syllable handling and validate all 15 styles individually with real approved material and visual/audio QC; record actual evidence, no automatic promotion.
4. Batch/retry/resume: sequential clip isolation, max two retryable retries, systemic stop and persisted checkpoint on VM/Drive, tested through failures and restart.
5. Delivery/cleanup: capability-aware origin delivery and safe cleanup only after successful delivery and resume evidence; test source, output and review evidence preservation.
