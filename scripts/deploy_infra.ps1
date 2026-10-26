<#
.SYNOPSIS
    DEPRECATED: Use scripts/cloudshell_deploy_middleware.sh instead.
.DESCRIPTION
    Direct local deployment via deploy_infra.ps1 is deprecated because it lacks
    multi-stack isolation, Firestore collection resolution, GCS remote state locks,
    and production plan approval safeguards.
#>

Write-Error @"
ERROR: scripts/deploy_infra.ps1 is deprecated.
Direct unreviewed deployment with hardcoded project defaults and auto-approve has been removed.
Please use the canonical Google Cloud Shell workflow:
  bash ./scripts/cloudshell_deploy_middleware.sh

See README.md for full deployment and go-live instructions.
"@
exit 1
