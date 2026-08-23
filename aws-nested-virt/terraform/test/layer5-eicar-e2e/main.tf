data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  prefix = "${var.project_name}-l5-${var.test_run_id}"
  tags = {
    Project   = var.project_name
    Stack     = "test-layer5-eicar-e2e"
    TestRunId = var.test_run_id
    Ephemeral = "true"
  }
}

# ---- VPC + public subnet (single-AZ; no ALB) -----------------------------

resource "aws_vpc" "test" {
  cidr_block           = "10.98.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${local.prefix}-vpc" })
}

resource "aws_internet_gateway" "test" {
  vpc_id = aws_vpc.test.id
  tags   = merge(local.tags, { Name = "${local.prefix}-igw" })
}

resource "aws_subnet" "test" {
  vpc_id                  = aws_vpc.test.id
  cidr_block              = "10.98.0.0/25"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${local.prefix}-subnet" })
}

resource "aws_route_table" "test" {
  vpc_id = aws_vpc.test.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.test.id
  }
  tags = local.tags
}

resource "aws_route_table_association" "test" {
  subnet_id      = aws_subnet.test.id
  route_table_id = aws_route_table.test.id
}

resource "aws_security_group" "test" {
  name        = "${local.prefix}-sg"
  description = "Layer 5 EICAR e2e test SG. Outbound any; SSH + CAPE web from admin CIDRs (debugging)."
  vpc_id      = aws_vpc.test.id

  dynamic "ingress" {
    for_each = length(var.allowed_admin_cidrs) > 0 ? [1] : []
    content {
      description = "SSH (debugging only; SSM is the primary access path)"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.allowed_admin_cidrs
    }
  }

  dynamic "ingress" {
    for_each = length(var.allowed_admin_cidrs) > 0 ? [1] : []
    content {
      description = "CAPE web UI (debugging EICAR submission failures)"
      from_port   = 8000
      to_port     = 8000
      protocol    = "tcp"
      cidr_blocks = var.allowed_admin_cidrs
    }
  }

  egress {
    description = "Outbound any - apt, freshclam, suricata-update, EC2/Secrets Manager APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

# ---- IAM: SSM + Secrets Manager (cape admin) -----------------------------

resource "aws_iam_role" "test" {
  name = "${local.prefix}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "test_ssm" {
  role       = aws_iam_role.test.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_secretsmanager_secret" "cape_admin" {
  count = var.cape_admin_secret_name != "" ? 1 : 0
  name  = var.cape_admin_secret_name
}

resource "aws_iam_role_policy" "test_secrets" {
  count = var.cape_admin_secret_name != "" ? 1 : 0

  name = "${local.prefix}-secrets"
  role = aws_iam_role.test.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [data.aws_secretsmanager_secret.cape_admin[0].arn]
    }]
  })
}

resource "aws_iam_instance_profile" "test" {
  name = "${local.prefix}-profile"
  role = aws_iam_role.test.name
  tags = local.tags
}

# ---- Instance ------------------------------------------------------------

resource "aws_instance" "test" {
  ami                         = var.cape_host_base_ami_id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.test.id
  vpc_security_group_ids      = [aws_security_group.test.id]
  iam_instance_profile        = aws_iam_instance_profile.test.name
  associate_public_ip_address = true
  key_name                    = var.key_pair_name != "" ? var.key_pair_name : null

  root_block_device {
    volume_size = 1000
    volume_type = "gp3"
    iops        = 16000
    throughput  = 1000
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = templatefile("${path.module}/userdata.sh", {
    apt_repo_url            = var.apt_repo_url
    apt_repo_keyring_url    = var.apt_repo_keyring_url
    cape_core_version       = var.cape_core_version
    cape_signatures_version = var.cape_signatures_version
    cape_qemu_version       = var.cape_qemu_version
    cape_suricata_version   = var.cape_suricata_version
    cape_admin_secret_arn   = var.cape_admin_secret_name != "" ? data.aws_secretsmanager_secret.cape_admin[0].arn : ""
    aws_region              = var.aws_region
  })

  tags = merge(local.tags, { Name = "${local.prefix}-host" })
}

# ---- Enable nested virtualization (mirrors nestedvirt-ami pattern) -------
#
# AWS requires the instance be stopped to flip CpuOptions.NestedVirtualization.
# Same flow as terraform/nestedvirt-ami: wait for SSM, pin cloud-init datasource
# to Ec2 (avoids a known DataSourceNone race), stop, modify, restart, wait
# instance-status-ok. The datasource pin makes user-data re-run on the
# post-restart boot so the apt overlay actually fires.

resource "terraform_data" "enable_nested_virtualization" {
  triggers_replace = {
    instance_id   = aws_instance.test.id
    aws_region    = var.aws_region
    instance_type = var.instance_type
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      INSTANCE_ID="${self.triggers_replace.instance_id}"
      REGION="${self.triggers_replace.aws_region}"
      INSTANCE_TYPE="${self.triggers_replace.instance_type}"

      CORE_COUNT=$(aws ec2 describe-instance-types \
        --instance-types "$INSTANCE_TYPE" \
        --region "$REGION" \
        --query 'InstanceTypes[0].VCpuInfo.DefaultCores' \
        --output text)
      THREADS_PER_CORE=$(aws ec2 describe-instance-types \
        --instance-types "$INSTANCE_TYPE" \
        --region "$REGION" \
        --query 'InstanceTypes[0].VCpuInfo.DefaultThreadsPerCore' \
        --output text)

      echo "[layer5] waiting for SSM agent..."
      for i in $(seq 1 30); do
        SSM_STATUS=$(aws ssm describe-instance-information \
          --filters Key=InstanceIds,Values="$INSTANCE_ID" \
          --region "$REGION" \
          --query 'InstanceInformationList[0].PingStatus' \
          --output text 2>/dev/null || echo None)
        [ "$SSM_STATUS" = "Online" ] && break
        sleep 10
      done

      echo "[layer5] pinning cloud-init datasource and cleaning state..."
      PREP_CMD=$(aws ssm send-command \
        --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript \
        --parameters '{"commands":["set -e","printf %s\\\\n \"datasource_list: [ Ec2, None ]\" | tee /etc/cloud/cloud.cfg.d/99-ec2.cfg","cloud-init clean --logs"]}' \
        --region "$REGION" \
        --query 'Command.CommandId' \
        --output text)
      sleep 10
      aws ssm get-command-invocation \
        --command-id "$PREP_CMD" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" \
        --query 'Status' \
        --output text

      aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null
      aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION"

      aws ec2 modify-instance-cpu-options \
        --instance-id "$INSTANCE_ID" \
        --core-count "$CORE_COUNT" \
        --threads-per-core "$THREADS_PER_CORE" \
        --nested-virtualization enabled \
        --region "$REGION"

      aws ec2 start-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null
      aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID" --region "$REGION"
    EOT
  }
}
