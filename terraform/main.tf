provider "aws" {
  region = "eu-west-3"
}

resource "aws_s3_bucket" "sauvegardes" {
  bucket = "crous-bot-sauvegardes-aymane"
}

resource "aws_s3_bucket_public_access_block" "prive" {
  bucket                  = aws_s3_bucket.sauvegardes.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "chiffrement" {
  bucket = aws_s3_bucket.sauvegardes.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "retention" {
  bucket = aws_s3_bucket.sauvegardes.id

  rule {
    id     = "supprimer-apres-30-jours"
    status = "Enabled"
    filter {}

    expiration {
      days = 30
    }
  }
}

resource "aws_iam_user" "vps" {
  name = "crous-vps-sauvegarde"
}

resource "aws_iam_user_policy" "ecriture_seule" {
  name = "ecriture-seule"
  user = aws_iam_user.vps.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.sauvegardes.arn}/*"
    }]
  })
}

resource "aws_iam_access_key" "vps" {
  user = aws_iam_user.vps.name
}

output "vps_access_key_id" {
  value = aws_iam_access_key.vps.id
}

output "vps_secret_access_key" {
  value     = aws_iam_access_key.vps.secret
  sensitive = true
}
