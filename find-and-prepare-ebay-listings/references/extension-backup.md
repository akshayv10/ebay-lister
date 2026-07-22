# Explicit Chrome-extension backup

Use this workflow only when the user explicitly requests the extension, backup, or revert path. The preserved extension source is `/Users/akballer47/Documents/ebay listing Chrome extension claude`. Never invoke it automatically after an API error or partial API mutation.

Follow [browser-workflow.md](browser-workflow.md) exactly. Keep its public button contract, global single-run ownership, one-click/no-retry rule, distinct eBay-tab binding, media settling, complete final-form audit, General 10%/Priority-off requirement, and `accepted -> extension_done -> ready_for_user` state path.

Use `scripts/extension_job.py` for extension states and reports. Never click eBay save or publish controls. Leave two audited forms open and unsaved for the user.

Before starting, confirm the current API run has made no account mutation. If API state is `payload_validated`, `api_prepared`, `publishing`, `publish_rolled_back`, `reconciliation_required`, or `live`, stop and require explicit user direction; do not create duplicate extension forms.
