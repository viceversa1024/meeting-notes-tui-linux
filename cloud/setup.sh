#!/bin/bash
# Meeting Notes — notes.harrywaterman.com infrastructure (S3 + CloudFront).
# Idempotent: safe to re-run. Two-phase:
#   run 1: creates bucket + requests the ACM cert, prints the validation
#          CNAME to add at Hover, exits.
#   run 2 (after the cert validates, ~5-30 min): creates the OAC,
#          CloudFront distribution, and bucket policy, prints the final
#          `notes` CNAME to add at Hover.
set -euo pipefail

DOMAIN="notes.harrywaterman.com"
BUCKET="notes-harrywaterman-com"
REGION="us-east-1"   # ACM certs used by CloudFront must live in us-east-1
OAC_NAME="meeting-notes-oac"
# Managed cache policy "CachingDisabled": unpublish must kill links
# instantly, and personal-scale traffic doesn't need caching.
CACHE_POLICY_ID="4135ea2d-6df8-44a3-9df3-4b5a84be39ad"

command -v aws >/dev/null || { echo "error: aws CLI not found" >&2; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Bucket (private, no listing) -------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
    echo "created bucket $BUCKET"
fi
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 2. Site assets ------------------------------------------------------------
aws s3 cp "$SCRIPT_DIR/site/404.html" "s3://$BUCKET/404.html" \
    --content-type "text/html; charset=utf-8"
aws s3 cp "$SCRIPT_DIR/site/robots.txt" "s3://$BUCKET/robots.txt" \
    --content-type "text/plain"

# 3. Certificate ------------------------------------------------------------
CERT_ARN="$(aws acm list-certificates --region us-east-1 \
    --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" \
    --output text)"
if [ "$CERT_ARN" = "None" ] || [ -z "$CERT_ARN" ]; then
    CERT_ARN="$(aws acm request-certificate --domain-name "$DOMAIN" \
        --validation-method DNS --region us-east-1 \
        --query CertificateArn --output text)"
    echo "requested certificate $CERT_ARN"
    sleep 10   # give ACM time to generate the validation record
fi
CERT_STATUS="$(aws acm describe-certificate --certificate-arn "$CERT_ARN" \
    --region us-east-1 --query Certificate.Status --output text)"
if [ "$CERT_STATUS" != "ISSUED" ]; then
    echo ""
    echo "== Certificate is $CERT_STATUS. Add this CNAME at Hover to validate =="
    aws acm describe-certificate --certificate-arn "$CERT_ARN" --region us-east-1 \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Name,Type,Value]' \
        --output text
    echo ""
    echo "Then re-run this script (validation usually takes 5-30 minutes)."
    exit 0
fi
echo "certificate ISSUED"

# 4. Origin Access Control --------------------------------------------------
OAC_ID="$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$OAC_NAME'].Id | [0]" \
    --output text)"
if [ "$OAC_ID" = "None" ] || [ -z "$OAC_ID" ]; then
    OAC_ID="$(aws cloudfront create-origin-access-control \
        --origin-access-control-config \
        "Name=$OAC_NAME,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
        --query OriginAccessControl.Id --output text)"
    echo "created origin access control $OAC_ID"
fi

# 5. Distribution -----------------------------------------------------------
DIST_ID="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Aliases.Items && contains(Aliases.Items, '$DOMAIN')].Id | [0]" \
    --output text)"
if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
    DIST_CONFIG="$(mktemp)"
    cat > "$DIST_CONFIG" <<EOF
{
  "CallerReference": "meeting-notes-$(date +%s)",
  "Comment": "meeting-notes unlisted note pages",
  "Enabled": true,
  "Aliases": {"Quantity": 1, "Items": ["$DOMAIN"]},
  "Origins": {"Quantity": 1, "Items": [{
      "Id": "s3-notes",
      "DomainName": "$BUCKET.s3.$REGION.amazonaws.com",
      "OriginAccessControlId": "$OAC_ID",
      "S3OriginConfig": {"OriginAccessIdentity": ""}
  }]},
  "DefaultCacheBehavior": {
      "TargetOriginId": "s3-notes",
      "ViewerProtocolPolicy": "redirect-to-https",
      "CachePolicyId": "$CACHE_POLICY_ID",
      "Compress": true
  },
  "CustomErrorResponses": {"Quantity": 2, "Items": [
      {"ErrorCode": 403, "ResponsePagePath": "/404.html",
       "ResponseCode": "404", "ErrorCachingMinTTL": 60},
      {"ErrorCode": 404, "ResponsePagePath": "/404.html",
       "ResponseCode": "404", "ErrorCachingMinTTL": 60}
  ]},
  "ViewerCertificate": {
      "ACMCertificateArn": "$CERT_ARN",
      "SSLSupportMethod": "sni-only",
      "MinimumProtocolVersion": "TLSv1.2_2021"
  }
}
EOF
    DIST_OUT="$(aws cloudfront create-distribution \
        --distribution-config "file://$DIST_CONFIG")"
    rm -f "$DIST_CONFIG"
    DIST_ID="$(echo "$DIST_OUT" | python3 -c \
        'import json,sys; print(json.load(sys.stdin)["Distribution"]["Id"])')"
    echo "created distribution $DIST_ID"
fi
DIST_DOMAIN="$(aws cloudfront get-distribution --id "$DIST_ID" \
    --query Distribution.DomainName --output text)"

# 6. Bucket policy: only this distribution may read -------------------------
POLICY="$(mktemp)"
cat > "$POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "cloudfront.amazonaws.com"},
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::$BUCKET/*",
    "Condition": {"StringEquals": {
      "AWS:SourceArn": "arn:aws:cloudfront::$ACCOUNT_ID:distribution/$DIST_ID"
    }}
  }]
}
EOF
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$POLICY"
rm -f "$POLICY"

echo ""
echo "== Done. Add this CNAME at Hover =="
echo "  notes  CNAME  $DIST_DOMAIN"
echo ""
echo "Then set in ~/.config/meeting-notes/config.yaml:"
echo "  upload_bucket: \"$BUCKET\""
echo "  upload_region: \"$REGION\""
echo "  upload_base_url: \"https://$DOMAIN\""
