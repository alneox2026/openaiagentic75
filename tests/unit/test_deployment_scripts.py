"""Regression checks for the template's safe multi-stack deployment defaults."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cloudshell_scripts_derive_an_isolated_stack_identity() -> None:
    build_script = (ROOT / "scripts" / "cloudshell_build_middleware.sh").read_text(
        encoding="utf-8"
    )
    deploy_script = (ROOT / "scripts" / "cloudshell_deploy_middleware.sh").read_text(
        encoding="utf-8"
    )

    assert 'MIDDLEWARE_STACK_NAME="${MIDDLEWARE_STACK_NAME:-$(basename "${ROOT_DIR}")}"' in build_script
    assert '${MIDDLEWARE_IMAGE_PREFIX}-gateway' in build_script
    assert 'TF_STATE_PREFIX="${TF_STATE_PREFIX:-stacks/${MIDDLEWARE_STACK_NAME}/middleware}"' in deploy_script
    assert '"gateway_service_name":' not in deploy_script
    assert "terraform_variable_default()" in deploy_script
    assert "terraform_variable_value()" in deploy_script
    assert "resolve_firestore_collection()" in deploy_script
    assert 'if [[ -v "${environment_name}" ]]; then' in deploy_script
    assert '[[ -n "${terraform_default}" && ! "${terraform_default}" =~ _v3$ ]]' in deploy_script
    assert 'resolve_firestore_collection FIRESTORE_CUSTOMER_WALLETS_COLLECTION firestore_customer_wallets_collection customer_wallets' in deploy_script
    assert '"firestore_customer_wallets_collection": "${FIRESTORE_CUSTOMER_WALLETS_COLLECTION}"' in deploy_script
    assert '"firestore_billing_reservations_collection": "${FIRESTORE_BILLING_RESERVATIONS_COLLECTION}"' in deploy_script
    assert 'ALLOW_LEGACY_RESOURCE_NAMES:-false' in deploy_script
    assert 'ceoagent-gateway-v3' in deploy_script


def test_worker_uses_stack_scoped_reservation_collection() -> None:
    locals_file = (ROOT / "infra" / "terraform" / "locals.tf").read_text(
        encoding="utf-8"
    )

    assert (
        "FIRESTORE_BILLING_RESERVATIONS_COLLECTION = "
        "var.firestore_billing_reservations_collection"
    ) in locals_file


def test_cloudshell_deploy_requires_explicit_recovery_and_delete_approval() -> None:
    deploy_script = (ROOT / "scripts" / "cloudshell_deploy_middleware.sh").read_text(
        encoding="utf-8"
    )
    import_script = (ROOT / "scripts" / "import_existing_resources.sh").read_text(
        encoding="utf-8"
    )

    assert 'IMPORT_EXISTING_RESOURCES:-false' in deploy_script
    assert 'ALLOW_TERRAFORM_DELETES:-false' in deploy_script
    assert 'Refusing to apply.' in deploy_script
    assert 'IMPORT_EXISTING_RESOURCES:-false' in import_script
    assert 'Refusing automatic import.' in import_script


def test_deploy_uses_temporary_terraform_files_without_overwriting_checkout() -> None:
    deploy_script = (ROOT / "scripts" / "cloudshell_deploy_middleware.sh").read_text(
        encoding="utf-8"
    )

    assert 'TF_TEMP_DIR="$(mktemp -d)"' in deploy_script
    assert 'cat > "${BACKEND_CONFIG_FILE}"' in deploy_script
    assert 'cat > "${TF_VARS_FILE}"' in deploy_script
    assert 'terraform init -backend-config="${BACKEND_CONFIG_FILE}" -reconfigure' in deploy_script
    assert 'terraform plan -var-file="${TF_VARS_FILE}"' in deploy_script
    assert 'rm -f terraform.auto.tfvars.json middleware.tfplan' not in deploy_script
    assert 'cat > backend.hcl' not in deploy_script


def test_test_wallet_helper_never_defaults_to_a_shared_collection() -> None:
    helper = (ROOT / "scripts" / "provision_test_wallet.py").read_text(encoding="utf-8")
    assert 'os.getenv("MIDDLEWARE_STACK_NAME", "")' in helper
    assert 'customer_wallets_v3' not in helper
    assert 'wallet_transactions_v3' not in helper


def test_cloudshell_deploy_shared_billing_defaults() -> None:
    deploy_script = (ROOT / "scripts" / "cloudshell_deploy_middleware.sh").read_text(
        encoding="utf-8"
    )

    assert "SHARE_CENTRAL_BILLING_COLLECTIONS:-true" in deploy_script
    assert 'terraform_variable_value "${terraform_name}"' in deploy_script
    assert 'FIRESTORE_CUSTOMER_WALLETS_COLLECTION="${FIRESTORE_CUSTOMER_WALLETS_COLLECTION:-$(terraform_variable_value firestore_customer_wallets_collection)}"' in deploy_script
    assert 'FIRESTORE_WALLET_TRANSACTIONS_COLLECTION="${FIRESTORE_WALLET_TRANSACTIONS_COLLECTION:-$(terraform_variable_value firestore_wallet_transactions_collection)}"' in deploy_script
    assert 'FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION="${FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION:-$(terraform_variable_value firestore_customer_billing_periods_collection)}"' in deploy_script
    assert 'FIRESTORE_CUSTOMER_WALLETS_COLLECTION="${FIRESTORE_CUSTOMER_WALLETS_COLLECTION:-customer_wallets_v3}"' not in deploy_script
    assert "ALLOW_CUSTOM_SHARED_COLLECTIONS:-false" in deploy_script
    assert "prevent wallet desynchronization from stale Cloud Shell variables" in deploy_script
    assert "exit 2" in deploy_script
    # Strict validation rejects non-true/false values
    assert "must be 'true' or 'false'" in deploy_script
    # Isolated sandbox fails closed on inherited collection overrides
    assert "SHARE_CENTRAL_BILLING_COLLECTIONS=false" in deploy_script
    assert "Unset ${shared_var} to use the stack-scoped name" in deploy_script


def _find_bash() -> "str | None":
    """Find a usable bash binary, skipping WSL bash on Windows."""
    import shutil

    for candidate in [
        shutil.which("bash"),
        "C:/Program Files/Git/bin/bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]:
        if candidate and Path(candidate).is_file():
            if "system32" not in str(candidate).lower():
                return str(candidate)
    return None


def _extract_functions_from_deploy_script() -> str:
    """Extract Firestore config resolvers from the actual deploy script
    from the actual deploy script so tests exercise real code."""
    deploy_text = (ROOT / "scripts" / "cloudshell_deploy_middleware.sh").read_text(
        encoding="utf-8"
    )
    # Extract function blocks (lines 22-51 in the script)
    lines = deploy_text.splitlines()
    extracted: list[str] = []
    in_function = False
    brace_depth = 0
    for line in lines:
        stripped = line.strip()
        if not in_function and (
            stripped.startswith("terraform_variable_default()")
            or stripped.startswith("terraform_variable_value()")
            or stripped.startswith("resolve_firestore_collection()")
        ):
            in_function = True
            brace_depth = 0
        if in_function:
            extracted.append(line)
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0 and "{" in "".join(extracted):
                in_function = False
                extracted.append("")
    return "\n".join(extracted)


def _extract_guard_block() -> str:
    """Extract the SHARE_CENTRAL_BILLING_COLLECTIONS guard block from the
    actual deploy script (from strict validation through 'fi')."""
    deploy_text = (ROOT / "scripts" / "cloudshell_deploy_middleware.sh").read_text(
        encoding="utf-8"
    )
    lines = deploy_text.splitlines()
    start = None
    fi_count_needed = 0
    extracted: list[str] = []
    for i, line in enumerate(lines):
        if start is None and "_SHARE_FLAG=" in line:
            start = i
        if start is not None:
            extracted.append(line)
            stripped = line.strip()
            if stripped.startswith("if ") or stripped.startswith("if ["):
                fi_count_needed += 1
            if stripped == "fi":
                fi_count_needed -= 1
                if fi_count_needed <= 0:
                    break
    return "\n".join(extracted)


import pytest


def test_shared_billing_collection_resolution_execution() -> None:
    """Sources the real deploy script functions to test collection resolution."""
    import subprocess
    import tempfile

    bash_bin = _find_bash()
    if not bash_bin:
        pytest.skip("No usable bash binary found")

    functions = _extract_functions_from_deploy_script()
    guard_block = _extract_guard_block()

    # Write the test script to a temp file to avoid f-string/bash brace conflicts
    test_script = "\n".join([
        "set -e",
        'FIRESTORE_NAMESPACE="stackA"',
        'ROOT_DIR="' + ROOT.as_posix() + '"',
        "",
        functions,
        "",
        "# --- Case 1: Clean shell with SHARE_CENTRAL_BILLING_COLLECTIONS=true ---",
        "unset FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
        "unset FIRESTORE_WALLET_TRANSACTIONS_COLLECTION",
        "unset FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION",
        "SHARE_CENTRAL_BILLING_COLLECTIONS=true",
        guard_block,
        'expected_shared_wallets="$(terraform_variable_value firestore_customer_wallets_collection)"',
        'res1="$(resolve_firestore_collection FIRESTORE_CUSTOMER_WALLETS_COLLECTION firestore_customer_wallets_collection customer_wallets)"',
        '[ "$res1" = "$expected_shared_wallets" ] || { echo "FAIL case 1: got $res1, expected $expected_shared_wallets"; exit 1; }',
        "",
        "# --- Case 2: SHARE_CENTRAL_BILLING_COLLECTIONS=false (isolated stack) ---",
        "unset FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
        "unset FIRESTORE_WALLET_TRANSACTIONS_COLLECTION",
        "unset FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION",
        "SHARE_CENTRAL_BILLING_COLLECTIONS=false",
        guard_block,
        'res2="$(resolve_firestore_collection FIRESTORE_CUSTOMER_WALLETS_COLLECTION firestore_customer_wallets_collection customer_wallets)"',
        '[ "$res2" = "customer_wallets_stackA" ] || { echo "FAIL case 2: got $res2"; exit 1; }',
        "",
        "# --- Case 3: Stale override with sharing=true should exit 2 ---",
        'export FIRESTORE_CUSTOMER_WALLETS_COLLECTION="customer_wallets_old_stack"',
        "SHARE_CENTRAL_BILLING_COLLECTIONS=true",
        "set +e",
        "(",
        "  set -e",
        "  " + guard_block.replace("\n", "\n  "),
        ")",
        "exit_code=$?",
        "set -e",
        '[ "$exit_code" -eq 2 ] || { echo "FAIL case 3: expected exit 2, got $exit_code"; exit 1; }',
        "",
        "# --- Case 4: Invalid flag value should exit 2 ---",
        "unset FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
        "SHARE_CENTRAL_BILLING_COLLECTIONS=TRUE",
        "set +e",
        "(",
        "  set -e",
        "  " + guard_block.replace("\n", "\n  "),
        ")",
        "exit_code=$?",
        "set -e",
        '[ "$exit_code" -eq 2 ] || { echo "FAIL case 4: expected exit 2 for invalid flag, got $exit_code"; exit 1; }',
        "",
        "# --- Case 5: Intentional custom shared override requires explicit opt-in ---",
        'export FIRESTORE_CUSTOMER_WALLETS_COLLECTION="customer_wallets_custom"',
        "export ALLOW_CUSTOM_SHARED_COLLECTIONS=true",
        "SHARE_CENTRAL_BILLING_COLLECTIONS=true",
        guard_block,
        'res5="$(resolve_firestore_collection FIRESTORE_CUSTOMER_WALLETS_COLLECTION firestore_customer_wallets_collection customer_wallets)"',
        '[ "$res5" = "customer_wallets_custom" ] || { echo "FAIL case 5: got $res5"; exit 1; }',
        "",
        "# --- Case 6: Isolated mode rejects a stale custom shared collection ---",
        'export FIRESTORE_CUSTOMER_WALLETS_COLLECTION="customer_wallets_service43"',
        "unset ALLOW_CUSTOM_SHARED_COLLECTIONS",
        "SHARE_CENTRAL_BILLING_COLLECTIONS=false",
        "set +e",
        "(",
        "  set -e",
        "  " + guard_block.replace("\n", "\n  "),
        ")",
        "exit_code=$?",
        "set -e",
        '[ "$exit_code" -eq 2 ] || { echo "FAIL case 6: expected exit 2 for custom collection in isolated mode, got $exit_code"; exit 1; }',
        "",
        "# --- Case 7: Explicit opt-in permits a deliberate isolated custom collection ---",
        'export FIRESTORE_CUSTOMER_WALLETS_COLLECTION="customer_wallets_isolated_custom"',
        "export ALLOW_CUSTOM_SHARED_COLLECTIONS=true",
        "SHARE_CENTRAL_BILLING_COLLECTIONS=false",
        guard_block,
        'res7="$(resolve_firestore_collection FIRESTORE_CUSTOMER_WALLETS_COLLECTION firestore_customer_wallets_collection customer_wallets)"',
        '[ "$res7" = "customer_wallets_isolated_custom" ] || { echo "FAIL case 7: got $res7"; exit 1; }',
        "",
        'echo "ALL CASES PASSED"',
    ])

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, encoding="utf-8"
    ) as f:
        f.write(test_script)
        script_path = f.name

    try:
        res = subprocess.run(
            [bash_bin, script_path], capture_output=True, text=True
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    assert res.returncode == 0, (
        f"Bash resolution test failed (exit {res.returncode}):\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    assert "ALL CASES PASSED" in res.stdout


@pytest.mark.parametrize("configured_in_tfvars", [True, False])
def test_shared_collection_resolver_honors_custom_terraform_values(
    tmp_path: Path,
    configured_in_tfvars: bool,
) -> None:
    import subprocess
    import tempfile

    bash_bin = _find_bash()
    if not bash_bin:
        pytest.skip("No usable bash binary found")

    tf_dir = tmp_path / "infra" / "terraform"
    tf_dir.mkdir(parents=True)
    configured_values = {
        "firestore_customer_wallets_collection": "customer_wallets_service43",
        "firestore_wallet_transactions_collection": "wallet_transactions_service43",
        "firestore_customer_billing_periods_collection": "customer_billing_periods_service43",
    }
    template_defaults = {
        "firestore_customer_wallets_collection": "customer_wallets_v3",
        "firestore_wallet_transactions_collection": "wallet_transactions_v3",
        "firestore_customer_billing_periods_collection": "customer_billing_periods_v3",
    }
    variable_defaults = template_defaults if configured_in_tfvars else configured_values
    (tf_dir / "variables.tf").write_text(
        "".join(
            f'variable "{name}" {{\n  default = "{value}"\n}}\n'
            for name, value in variable_defaults.items()
        ),
        encoding="utf-8",
    )
    if configured_in_tfvars:
        (tf_dir / "terraform.tfvars").write_text(
            "".join(
                f'{name} = "{value}"\n'
                for name, value in configured_values.items()
            ),
            encoding="utf-8",
        )

    functions = _extract_functions_from_deploy_script()
    guard_block = _extract_guard_block()
    script = "\n".join(
        [
            "set -e",
            f'ROOT_DIR="{tmp_path.as_posix()}"',
            'FIRESTORE_NAMESPACE="agentic43"',
            "unset FIRESTORE_CUSTOMER_WALLETS_COLLECTION FIRESTORE_WALLET_TRANSACTIONS_COLLECTION FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION",
            functions,
            'SHARE_CENTRAL_BILLING_COLLECTIONS="true"',
            guard_block,
            '[ "$FIRESTORE_CUSTOMER_WALLETS_COLLECTION" = "customer_wallets_service43" ]',
            '[ "$FIRESTORE_WALLET_TRANSACTIONS_COLLECTION" = "wallet_transactions_service43" ]',
            '[ "$FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION" = "customer_billing_periods_service43" ]',
            'echo "CUSTOM TERRAFORM COLLECTIONS HONORED"',
        ]
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, encoding="utf-8") as f:
        f.write(script)
        script_path = f.name
    try:
        result = subprocess.run([bash_bin, script_path], capture_output=True, text=True)
    finally:
        Path(script_path).unlink(missing_ok=True)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "CUSTOM TERRAFORM COLLECTIONS HONORED" in result.stdout
