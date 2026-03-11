import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import click
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console
from rich.panel import Panel

from brr.state import ensure_state_dirs, read_config, write_config, CONFIG_PATH, KEYS_DIR

console = Console()

NEBIUS_CREDS_PATH = Path.home() / ".nebius" / "credentials.json"

_cached_sdk = None


def _check_credentials():
    """Verify Nebius credentials exist (CLI profile or credentials file)."""
    cli_config = Path.home() / ".nebius" / "config.yaml"
    if cli_config.exists():
        console.print(f"Nebius CLI profile: [green]{cli_config}[/green]")
        return True
    if NEBIUS_CREDS_PATH.exists():
        console.print(f"Nebius credentials: [green]{NEBIUS_CREDS_PATH}[/green]")
        return True
    if os.environ.get("NEBIUS_IAM_TOKEN"):
        console.print("Nebius auth via [green]NEBIUS_IAM_TOKEN[/green] env var")
        return True

    console.print("[red]No Nebius credentials found[/red]")
    console.print("  Run [bold]nebius auth login[/bold] to authenticate")
    return False


def _nebius_sdk():
    """Return a cached Nebius SDK for the configure wizard.

    Tries (in order):
    1. CLI access token via `nebius iam get-access-token` (personal credentials)
    2. CLI profile via Config() (needs federation-id in profile)
    3. credentials.json (service account key — limited IAM permissions)
    """
    global _cached_sdk
    if _cached_sdk is not None:
        return _cached_sdk

    from nebius.sdk import SDK

    # Try getting an access token from the CLI — works regardless of profile format
    try:
        result = subprocess.run(
            ["nebius", "iam", "get-access-token"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            _cached_sdk = SDK(credentials=result.stdout.strip())
            return _cached_sdk
    except Exception:
        pass

    try:
        from nebius.aio.cli_config import Config
        _cached_sdk = SDK(config_reader=Config())
        return _cached_sdk
    except Exception:
        pass

    if NEBIUS_CREDS_PATH.exists():
        Console(stderr=True).print(
            "[yellow]Warning:[/yellow] Using service account credentials.\n"
            "  IAM operations (permissions, keys) may fail — SA credentials lack IAM admin access.\n"
            "  Install the Nebius CLI and authenticate for full access."
        )
        _cached_sdk = SDK(credentials_file_name=str(NEBIUS_CREDS_PATH))
        return _cached_sdk

    Console(stderr=True).print(
        "[yellow]Warning:[/yellow] No Nebius credentials found. API calls will likely fail."
    )
    _cached_sdk = SDK()
    return _cached_sdk


def _get_or_create_security_group(project_id, subnet_id):
    """Get or create a 'brr-cluster' security group for the subnet's network.

    Mirrors the AWS pattern: SSH from anywhere + all traffic within the group.
    """
    try:
        import asyncio
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.vpc.v1 import (
            SubnetServiceClient,
            GetSubnetRequest,
            SecurityGroupServiceClient,
            ListSecurityGroupsRequest,
            CreateSecurityGroupRequest,
            SecurityGroupSpec,
            SecurityRuleServiceClient,
            CreateSecurityRuleRequest,
            SecurityRuleSpec,
            RuleIngress,
            RuleAccessAction,
            RuleProtocol,
            RuleType,
        )

        async def _get_or_create():
            sdk = _nebius_sdk()

            # Get network_id from subnet
            subnet_client = SubnetServiceClient(sdk)
            subnet = await subnet_client.get(GetSubnetRequest(id=subnet_id))
            network_id = subnet.spec.network_id

            # Look for existing brr-cluster security group
            sg_client = SecurityGroupServiceClient(sdk)
            resp = await sg_client.list(ListSecurityGroupsRequest(parent_id=project_id))
            for sg in resp.items:
                if sg.metadata.name == "brr-cluster":
                    return sg.metadata.id

            # Create security group
            op = await sg_client.create(CreateSecurityGroupRequest(
                metadata=ResourceMetadata(
                    parent_id=project_id,
                    name="brr-cluster",
                ),
                spec=SecurityGroupSpec(network_id=network_id),
            ))
            await op.wait()
            sg_id = op.resource_id

            # Add rules concurrently
            rule_client = SecurityRuleServiceClient(sdk)

            # SSH from anywhere
            ssh_ingress = RuleIngress(source_cidrs=["0.0.0.0/0"])
            ssh_ingress.destination_ports.append(22)
            ssh_op = await rule_client.create(CreateSecurityRuleRequest(
                metadata=ResourceMetadata(parent_id=sg_id),
                spec=SecurityRuleSpec(
                    access=RuleAccessAction.ALLOW,
                    protocol=RuleProtocol.TCP,
                    ingress=ssh_ingress,
                    type=RuleType.STATEFUL,
                    priority=100,
                ),
            ))

            # All traffic within the security group
            mesh_op = await rule_client.create(CreateSecurityRuleRequest(
                metadata=ResourceMetadata(parent_id=sg_id),
                spec=SecurityRuleSpec(
                    access=RuleAccessAction.ALLOW,
                    protocol=RuleProtocol.ANY,
                    ingress=RuleIngress(
                        source_security_group_id=sg_id,
                    ),
                    type=RuleType.STATEFUL,
                    priority=200,
                ),
            ))

            await asyncio.gather(ssh_op.wait(), mesh_op.wait())

            return sg_id

        return asyncio.run(_get_or_create())
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Failed to set up security group: {e}")
        return None


def _list_subnets(project_id):
    """Try to list subnets via Nebius SDK. Returns list of (id, name, zone) or None."""
    try:
        import asyncio
        from nebius.api.nebius.vpc.v1 import SubnetServiceClient, ListSubnetsRequest

        async def _list():
            sdk = _nebius_sdk()
            client = SubnetServiceClient(sdk)
            resp = await client.list(ListSubnetsRequest(parent_id=project_id))
            subnets = []
            for s in resp.items:
                name = s.metadata.name if s.metadata.name else "(unnamed)"
                zone = ""
                if hasattr(s.spec, "zone"):
                    zone = s.spec.zone
                subnets.append((s.metadata.id, name, zone))
            return subnets

        return asyncio.run(_list())
    except Exception as e:
        Console(stderr=True).print(f"[yellow]Warning:[/yellow] Failed to list subnets: {e}")
        return None


def _list_filesystems(project_id):
    """List existing shared filesystems. Returns list of (id, name, size_gb) or None."""
    try:
        import asyncio
        from nebius.api.nebius.compute.v1 import FilesystemServiceClient, ListFilesystemsRequest

        async def _list():
            sdk = _nebius_sdk()
            client = FilesystemServiceClient(sdk)
            resp = await client.list(ListFilesystemsRequest(parent_id=project_id))
            filesystems = []
            for fs in resp.items:
                name = fs.metadata.name if fs.metadata.name else "(unnamed)"
                size_gb = getattr(fs.spec, "size_gibibytes", 0) or 0
                filesystems.append((fs.metadata.id, name, size_gb))
            return filesystems

        return asyncio.run(_list())
    except Exception as e:
        Console(stderr=True).print(f"[yellow]Warning:[/yellow] Failed to list filesystems: {e}")
        return None


def _create_filesystem(project_id, name, size_gb):
    """Create a shared filesystem. Returns filesystem ID or None."""
    try:
        import asyncio
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.compute.v1 import (
            FilesystemServiceClient,
            CreateFilesystemRequest,
            FilesystemSpec,
        )

        async def _create():
            sdk = _nebius_sdk()
            client = FilesystemServiceClient(sdk)
            op = await client.create(CreateFilesystemRequest(
                metadata=ResourceMetadata(
                    parent_id=project_id,
                    name=name,
                ),
                spec=FilesystemSpec(
                    type=FilesystemSpec.FilesystemType.NETWORK_SSD,
                    size_gibibytes=size_gb,
                ),
            ))
            await op.wait()
            return op.resource_id

        return asyncio.run(_create())
    except Exception as e:
        console.print(f"[red]Failed to create filesystem: {e}[/red]")
        return None


def _resize_filesystem(filesystem_id, new_size_gb):
    """Resize an existing shared filesystem. Returns True on success."""
    try:
        import asyncio
        from nebius.api.nebius.compute.v1 import (
            FilesystemServiceClient,
            GetFilesystemRequest,
            UpdateFilesystemRequest,
            FilesystemSpec,
        )

        async def _resize():
            sdk = _nebius_sdk()
            client = FilesystemServiceClient(sdk)
            fs = await client.get(GetFilesystemRequest(id=filesystem_id))
            op = await client.update(UpdateFilesystemRequest(
                metadata=fs.metadata,
                spec=FilesystemSpec(
                    type=fs.spec.type,
                    size_gibibytes=new_size_gb,
                ),
            ))
            await op.wait()
            return True

        return asyncio.run(_resize())
    except Exception as e:
        console.print(f"[red]Failed to resize filesystem: {e}[/red]")
        return False


def _list_service_accounts(project_id):
    """List existing service accounts. Returns list of (id, name) or None."""
    try:
        import asyncio
        from nebius.api.nebius.iam.v1 import ServiceAccountServiceClient, ListServiceAccountRequest

        async def _list():
            sdk = _nebius_sdk()
            client = ServiceAccountServiceClient(sdk)
            resp = await client.list(ListServiceAccountRequest(parent_id=project_id))
            accounts = []
            for sa in resp.items:
                name = sa.metadata.name if sa.metadata.name else "(unnamed)"
                accounts.append((sa.metadata.id, name))
            return accounts

        return asyncio.run(_list())
    except Exception as e:
        Console(stderr=True).print(f"[yellow]Warning:[/yellow] Failed to list service accounts: {e}")
        return None


def _create_service_account(project_id, name):
    """Create a service account. Returns SA ID or None."""
    try:
        import asyncio
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.iam.v1 import (
            ServiceAccountServiceClient,
            CreateServiceAccountRequest,
        )

        async def _create():
            sdk = _nebius_sdk()
            client = ServiceAccountServiceClient(sdk)
            op = await client.create(CreateServiceAccountRequest(
                metadata=ResourceMetadata(
                    parent_id=project_id,
                    name=name,
                ),
            ))
            await op.wait()
            return op.resource_id

        return asyncio.run(_create())
    except Exception as e:
        console.print(f"[red]Failed to create service account: {e}[/red]")
        return None


def _add_to_editors_group(project_id, sa_id):
    """Add a service account to the editors group and grant it admin on itself.

    The SA needs editor permissions for compute + storage operations.
    It also needs admin scoped to its own SA resource so it can attach
    itself to instances it creates (required for Object Storage access).
    """
    try:
        import asyncio
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.iam.v1 import (
            ProjectServiceClient,
            GetProjectRequest,
            GroupServiceClient,
            GetGroupByNameRequest,
            GroupMembershipServiceClient,
            CreateGroupMembershipRequest,
            GroupMembershipSpec,
            AccessPermitServiceClient,
            CreateAccessPermitRequest,
            AccessPermitSpec,
        )

        async def _add():
            sdk = _nebius_sdk()
            # Get tenant ID from project
            project_client = ProjectServiceClient(sdk)
            project = await project_client.get(GetProjectRequest(id=project_id))
            tenant_id = project.metadata.parent_id

            # Find editors group
            group_client = GroupServiceClient(sdk)
            group = await group_client.get_by_name(GetGroupByNameRequest(
                parent_id=tenant_id,
                name="editors",
            ))

            # Add SA as member of editors group
            membership_client = GroupMembershipServiceClient(sdk)
            try:
                op = await membership_client.create(CreateGroupMembershipRequest(
                    metadata=ResourceMetadata(parent_id=group.metadata.id),
                    spec=GroupMembershipSpec(member_id=sa_id),
                ))
                await op.wait()
            except Exception as e:
                if "ALREADY_EXISTS" not in str(e):
                    raise

            # Grant admin scoped to the SA itself so it can attach
            # itself to instances (editors role alone doesn't allow this)
            permit_client = AccessPermitServiceClient(sdk)
            try:
                op = await permit_client.create(CreateAccessPermitRequest(
                    metadata=ResourceMetadata(parent_id=group.metadata.id),
                    spec=AccessPermitSpec(
                        resource_id=sa_id,
                        role="admin",
                    ),
                ))
                await op.wait()
            except Exception as e:
                if "ALREADY_EXISTS" not in str(e):
                    raise

            return True

        return asyncio.run(_add())
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Failed to add SA to editors group: {e}")
        return False


def _create_service_account_key(project_id, sa_id):
    """Generate an RSA key pair, upload the public key, and write credentials.json.

    Returns True on success.
    """
    try:
        import asyncio
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.iam.v1 import (
            AuthPublicKeyServiceClient,
            CreateAuthPublicKeyRequest,
            AuthPublicKeySpec,
            Account,
        )

        async def _create():
            # Generate RSA key pair
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            private_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
            public_pem = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode()

            # Upload public key
            sdk = _nebius_sdk()
            client = AuthPublicKeyServiceClient(sdk)
            op = await client.create(CreateAuthPublicKeyRequest(
                metadata=ResourceMetadata(parent_id=project_id),
                spec=AuthPublicKeySpec(
                    account=Account(
                        service_account=Account.ServiceAccount(id=sa_id),
                    ),
                    data=public_pem,
                    description="brr cluster autoscaling",
                ),
            ))
            await op.wait()
            key_id = op.resource_id

            # Write credentials.json
            creds = {
                "subject-credentials": {
                    "type": "JWT",
                    "alg": "RS256",
                    "private-key": private_pem,
                    "kid": key_id,
                    "iss": sa_id,
                    "sub": sa_id,
                }
            }
            NEBIUS_CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
            NEBIUS_CREDS_PATH.write_text(json.dumps(creds, indent=2) + "\n")
            os.chmod(NEBIUS_CREDS_PATH, 0o600)
            return True

        return asyncio.run(_create())
    except Exception as e:
        console.print(f"[red]Failed to create service account key: {e}[/red]")
        return False


def _create_s3_access_key(project_id, sa_id):
    """Create an S3-compatible access key for Object Storage.

    Returns (aws_access_key_id, secret) or None on failure.
    """
    try:
        import asyncio
        from nebius.api.nebius.common.v1 import ResourceMetadata
        from nebius.api.nebius.iam.v1 import Account
        from nebius.api.nebius.iam.v2 import (
            AccessKeyServiceClient,
            CreateAccessKeyRequest,
            AccessKeySpec,
            GetAccessKeySecretRequest,
        )

        async def _create():
            sdk = _nebius_sdk()
            client = AccessKeyServiceClient(sdk)
            op = await client.create(CreateAccessKeyRequest(
                metadata=ResourceMetadata(parent_id=project_id),
                spec=AccessKeySpec(
                    account=Account(
                        service_account=Account.ServiceAccount(id=sa_id),
                    ),
                    description="brr object storage",
                ),
            ))
            await op.wait()
            key_id = op.resource_id

            # Fetch the AWS key ID and secret (only available once)
            secret_resp = await client.get_secret(
                GetAccessKeySecretRequest(id=key_id)
            )
            return (secret_resp.aws_access_key_id, secret_resp.secret)

        return asyncio.run(_create())
    except Exception as e:
        console.print(f"[red]Failed to create S3 access key: {e}[/red]")
        return None


def _get_or_create_ssh_key():
    """Find an existing Nebius SSH key or generate a new one."""
    ensure_state_dirs()

    # Check for existing Nebius keys
    local_keys = sorted(
        f for f in os.listdir(KEYS_DIR)
        if f.startswith("nebius-") and not f.endswith(".pub")
    )
    if local_keys:
        key_path = str(KEYS_DIR / local_keys[0])
        console.print(f"Using existing SSH key: [green]{local_keys[0]}[/green]")
        return key_path

    # Generate new keypair
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key_name = f"nebius-{timestamp}"
    key_path = str(KEYS_DIR / key_name)

    console.print(f"Generating SSH key: [bold cyan]{key_name}[/bold cyan]...")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-C", "brr-nebius"],
        check=True, capture_output=True,
    )
    os.chmod(key_path, stat.S_IRUSR)

    console.print(f"SSH key: [green]{key_path}[/green]")
    console.print(f"Public key: [green]{key_path}.pub[/green]")
    return key_path


def _setup_github_ssh(ssh_key):
    """Add SSH public key to GitHub and return the private key path for file_mounts.

    Returns the private key path (for GITHUB_SSH_KEY config), or empty string on failure.
    """
    # Derive public key
    pubkey_result = subprocess.run(
        ["ssh-keygen", "-y", "-f", ssh_key], capture_output=True, text=True
    )
    if pubkey_result.returncode != 0:
        console.print(f"[red]Failed to derive public key: {pubkey_result.stderr.strip()}[/red]")
        return ssh_key  # Still return path so key is copied for GitHub access

    if not shutil.which("gh"):
        console.print("[yellow]gh CLI not found — skipping GitHub key registration[/yellow]")
        console.print("[yellow]Install gh and run 'brr configure nebius' again to add the key[/yellow]")
        return ssh_key

    auth_check = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if auth_check.returncode != 0:
        console.print("[yellow]gh CLI not authenticated — skipping GitHub key registration[/yellow]")
        console.print("[yellow]Run 'gh auth login' and then 'brr configure nebius' again[/yellow]")
        return ssh_key

    # Check if key already registered
    list_result = subprocess.run(
        ["gh", "ssh-key", "list"], capture_output=True, text=True
    )
    if list_result.returncode == 0:
        for line in list_result.stdout.splitlines():
            if "brr-nebius" in line:
                console.print("SSH key already registered on GitHub: [green]brr-nebius[/green]")
                return ssh_key

    # Add public key to GitHub
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as tmp:
        tmp.write(pubkey_result.stdout)
        tmp_path = tmp.name

    try:
        add_result = subprocess.run(
            ["gh", "ssh-key", "add", tmp_path, "--title", "brr-nebius"],
            capture_output=True, text=True,
        )
        if add_result.returncode == 0:
            console.print("Added SSH key to GitHub: [green]brr-nebius[/green]")
        else:
            console.print(f"[red]Failed to add key to GitHub: {add_result.stderr.strip()}[/red]")
    finally:
        os.unlink(tmp_path)

    return ssh_key


def configure_nebius():
    """Interactive Nebius configuration wizard."""
    ensure_state_dirs()
    existing = read_config()

    console.print(Panel("Nebius configuration", title="brr configure", border_style="cyan"))

    # --- Credentials ---
    if not _check_credentials():
        raise click.Abort()
    console.print()

    # --- Project ID ---
    project_id = click.prompt(
        "Nebius project ID",
        default=existing.get("NEBIUS_PROJECT_ID", ""),
    )

    # --- Subnet ---
    subnets = _list_subnets(project_id)
    if subnets:
        default_subnet = existing.get("NEBIUS_SUBNET_ID", subnets[0][0])
        subnet_choices = []
        for sid, name, zone in subnets:
            label = f"{name} ({sid})"
            if zone:
                label += f" — {zone}"
            subnet_choices.append(Choice(value=sid, name=label))

        subnet_id = inquirer.select(
            message="Select subnet",
            choices=subnet_choices,
            default=default_subnet if default_subnet in [s[0] for s in subnets] else None,
        ).execute()
    else:
        subnet_id = click.prompt(
            "Nebius subnet ID",
            default=existing.get("NEBIUS_SUBNET_ID", ""),
        )

    # --- Security group ---
    console.print()
    with console.status("[bold green]Setting up security group..."):
        security_group_id = _get_or_create_security_group(project_id, subnet_id) or ""
    if security_group_id:
        console.print(f"Security group: [green]{security_group_id}[/green]")
    else:
        console.print("[yellow]No security group configured — instances will have no firewall rules[/yellow]")

    # --- SSH key ---
    ssh_key = existing.get("NEBIUS_SSH_KEY", "")
    if ssh_key and Path(ssh_key).exists():
        console.print(f"\nUsing existing SSH key: [green]{ssh_key}[/green]")
        if click.confirm("Generate a new key instead?", default=False):
            ssh_key = _get_or_create_ssh_key()
    else:
        console.print()
        ssh_key = _get_or_create_ssh_key()

    # --- Shared filesystem ---
    console.print()
    filesystem_id = existing.get("NEBIUS_FILESYSTEM_ID", "")
    if click.confirm(
        "Set up shared filesystem for persistent ~/code?",
        default=bool(filesystem_id),
    ):
        filesystems = _list_filesystems(project_id)
        fs_choices = []
        if filesystems:
            for fid, name, size_gb in filesystems:
                fs_choices.append(Choice(value=fid, name=f"{name} ({fid}) — {size_gb} GB"))
        fs_choices.append(Choice(value="_create", name="Create new filesystem"))

        default_fs = filesystem_id if filesystem_id in [f[0] for f in (filesystems or [])] else None
        choice = inquirer.select(
            message="Select filesystem",
            choices=fs_choices,
            default=default_fs,
        ).execute()

        if choice == "_create":
            fs_name = click.prompt("Filesystem name", default="brr-shared")
            fs_size = click.prompt("Size (GB)", default=100, type=int)
            with console.status("[bold green]Creating filesystem..."):
                filesystem_id = _create_filesystem(project_id, fs_name, fs_size) or ""
            if filesystem_id:
                console.print(f"Created filesystem: [green]{filesystem_id}[/green]")
        else:
            filesystem_id = choice
            # Offer to resize the selected filesystem
            current_size = next(
                (s for fid, _, s in (filesystems or []) if fid == choice), None
            )
            if current_size is not None:
                new_size = click.prompt(
                    f"  Filesystem size (GB, currently {current_size})",
                    default=current_size,
                    type=int,
                )
                if new_size < current_size:
                    console.print("[yellow]Nebius filesystems can only grow, not shrink — keeping current size[/yellow]")
                elif new_size > current_size:
                    with console.status("[bold green]Resizing filesystem..."):
                        if _resize_filesystem(filesystem_id, new_size):
                            console.print(f"Resized to [green]{new_size} GB[/green]")
                        else:
                            console.print("[yellow]Resize failed — keeping current size[/yellow]")
    else:
        filesystem_id = ""

    # --- Service account ---
    console.print()
    s3_key_id = existing.get("NEBIUS_S3_ACCESS_KEY_ID", "")
    s3_secret_key = existing.get("NEBIUS_S3_SECRET_KEY", "")
    service_account_id = existing.get("NEBIUS_SERVICE_ACCOUNT_ID", "")

    accounts = _list_service_accounts(project_id)
    sa_choices = []
    if accounts:
        for said, name in accounts:
            sa_choices.append(Choice(value=said, name=f"{name} ({said})"))
    sa_choices.append(Choice(value="_create", name="Create new service account"))

    default_sa = service_account_id if service_account_id in [a[0] for a in (accounts or [])] else None
    choice = inquirer.select(
        message="Select service account",
        choices=sa_choices,
        default=default_sa,
    ).execute()

    created = False
    if choice == "_create":
        sa_name = click.prompt("Service account name", default="brr-cluster")
        with console.status("[bold green]Creating service account..."):
            service_account_id = _create_service_account(project_id, sa_name) or ""
        if service_account_id:
            console.print(f"Created service account: [green]{service_account_id}[/green]")
            created = True
    else:
        service_account_id = choice

    if service_account_id:
        # Permissions (editors group + scoped admin access permit)
        with console.status("[bold green]Setting up permissions..."):
            if _add_to_editors_group(project_id, service_account_id):
                console.print("Added to [green]editors[/green] group with SA access permit")

        # Credentials key for node provider (autoscaling)
        needs_creds = created or not NEBIUS_CREDS_PATH.exists()
        if not needs_creds and NEBIUS_CREDS_PATH.exists():
            try:
                creds = json.loads(NEBIUS_CREDS_PATH.read_text())
                needs_creds = creds.get("subject-credentials", {}).get("iss", "") != service_account_id
            except (json.JSONDecodeError, KeyError):
                needs_creds = True
        if needs_creds:
            with console.status("[bold green]Generating credentials key..."):
                if _create_service_account_key(project_id, service_account_id):
                    console.print(f"Wrote credentials: [green]{NEBIUS_CREDS_PATH}[/green]")

        # S3 access key for Object Storage
        # Recreate if missing or if SA changed (needs_creds implies SA mismatch)
        if not s3_key_id or needs_creds:
            with console.status("[bold green]Creating S3 access key..."):
                result = _create_s3_access_key(project_id, service_account_id)
            if result:
                s3_key_id, s3_secret_key = result
                console.print(f"S3 access key: [green]{s3_key_id}[/green]")

    # --- GitHub SSH access ---
    console.print()
    github_ssh_key = existing.get("GITHUB_SSH_KEY", "")
    if click.confirm(
        "Set up GitHub SSH access for clusters?",
        default=bool(github_ssh_key),
    ):
        github_ssh_key = _setup_github_ssh(ssh_key)
    else:
        github_ssh_key = ""

    # --- Write config (merge with existing) ---
    updates = {
        "NEBIUS_PROJECT_ID": project_id,
        "NEBIUS_SUBNET_ID": subnet_id,
        "NEBIUS_SSH_KEY": ssh_key,
        "NEBIUS_FILESYSTEM_ID": filesystem_id,
        "NEBIUS_SECURITY_GROUP_ID": security_group_id,
        "NEBIUS_SERVICE_ACCOUNT_ID": service_account_id,
        "NEBIUS_S3_ACCESS_KEY_ID": s3_key_id,
        "NEBIUS_S3_SECRET_KEY": s3_secret_key,
        "GITHUB_SSH_KEY": github_ssh_key,
    }

    merged = dict(existing)
    merged.update(updates)
    write_config(merged)
    console.print(f"\nWrote [green]{CONFIG_PATH}[/green]")

    console.print()
    console.print("[bold green]Done![/bold green] Next steps:")
    console.print("  brr configure tools                         # select AI coding tools")
    console.print("  brr configure general                       # instance settings")
    console.print("  brr up nebius:h100                          # launch H100 GPU cluster")
