# S3 Media Files Setup Guide

## Why S3?
- **Scalable**: No storage limits on EB instances
- **Reliable**: Files persist even if EB instances restart
- **Fast**: S3 + CloudFront provides global CDN
- **Cost-effective**: Pay only for storage and requests
- **HTTPS**: S3 supports HTTPS natively

## Step 1: Create S3 Bucket

1. Go to **AWS S3 Console** → **Create bucket**
2. **Bucket name**: `cinematch-media-028210901732` (use your account ID for uniqueness)
3. **AWS Region**: `us-east-1` (same as your EB environment)
4. **Object Ownership**: `ACLs disabled` (recommended)
5. **Block Public Access**: **Uncheck all** (we need public read access for images)
   - ⚠️ **Important**: Uncheck "Block all public access"
6. **Bucket Versioning**: Disable (unless you need it)
7. **Default encryption**: Enable (SSE-S3 is fine)
8. Click **"Create bucket"**

## Step 2: Configure Bucket Policy

1. Go to your bucket → **Permissions** tab
2. Scroll to **Bucket policy**
3. Click **Edit** and add this policy:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::cinematch-media-028210901732/*"
        }
    ]
}
```

**Replace `cinematch-media-028210901732` with your actual bucket name.**

4. Click **Save changes**

## Step 3: Configure CORS (if needed)

1. Go to **Permissions** tab → **Cross-origin resource sharing (CORS)**
2. Click **Edit** and add:

```json
[
    {
        "AllowedHeaders": ["*"],
        "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"],
        "AllowedOrigins": [
            "https://dz8ddbc7adyzz.cloudfront.net",
            "https://cinematch-production.eba-mixpvdvh.us-east-1.elasticbeanstalk.com"
        ],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000
    }
]
```

3. Click **Save changes**

## Step 4: Create IAM User/Policy for Django

### Option A: Use Existing EB Instance Profile (Recommended)

EB instances already have IAM roles. We'll add S3 permissions to the existing role.

1. Go to **EC2 Console** → **Instances**
2. Find your EB instance → Click on it
3. Go to **Security** tab → Click on **IAM role** link
4. Click **Add permissions** → **Create inline policy**
5. Use JSON editor and add:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
                "s3:GetObject",
                "s3:DeleteObject",
                "s3:ListBucket"
            ],
            "Resource": [
                "arn:aws:s3:::cinematch-media-028210901732",
                "arn:aws:s3:::cinematch-media-028210901732/*"
            ]
        }
    ]
}
```

6. Name it: `S3MediaAccess`
7. Click **Create policy**

### Option B: Create IAM User (Alternative)

1. Go to **IAM Console** → **Users** → **Create user**
2. User name: `cinematch-s3-media`
3. **Access type**: Programmatic access
4. **Permissions**: Attach policy directly → **Create policy**
5. Use JSON editor with the policy above
6. Save and attach to user
7. **Save the Access Key ID and Secret Access Key** (you'll need them)

## Step 5: Update Environment Variables

Add these to your EB environment:

```bash
AWS_STORAGE_BUCKET_NAME=cinematch-media-028210901732
AWS_S3_REGION_NAME=us-east-1
AWS_S3_CUSTOM_DOMAIN=cinematch-media-028210901732.s3.amazonaws.com
# If using IAM user (Option B), also add:
# AWS_ACCESS_KEY_ID=your-access-key
# AWS_SECRET_ACCESS_KEY=your-secret-key
```

**To set via EB CLI:**
```bash
eb setenv AWS_STORAGE_BUCKET_NAME=cinematch-media-028210901732 AWS_S3_REGION_NAME=us-east-1 AWS_S3_CUSTOM_DOMAIN=cinematch-media-028210901732.s3.amazonaws.com
```

## Step 6: Install Dependencies

The code will automatically install `boto3` and `django-storages` when deployed.

## Step 7: Migrate Existing Files (Optional)

If you have existing profile images on EB:

1. SSH into your EB instance or use EB CLI:
   ```bash
   eb ssh
   ```

2. Install AWS CLI (if not installed):
   ```bash
   pip install awscli
   ```

3. Sync files to S3:
   ```bash
   aws s3 sync /var/app/current/media/profile_images/ s3://cinematch-media-028210901732/profile_images/ --region us-east-1
   ```

## Step 8: Configure CloudFront for S3 (Optional but Recommended)

For better performance, you can create a CloudFront distribution for your S3 bucket:

1. Go to **CloudFront** → **Create distribution**
2. **Origin domain**: Select your S3 bucket (`cinematch-media-028210901732.s3.amazonaws.com`)
3. **Origin access**: Use OAC (Origin Access Control) or keep it simple
4. **Viewer protocol policy**: Redirect HTTP to HTTPS
5. **Create distribution**
6. Update `AWS_S3_CUSTOM_DOMAIN` to use CloudFront domain instead

## Verification

After deployment:

1. Upload a new profile image
2. Check S3 bucket - file should appear there
3. Check image URL - should be `https://cinematch-media-028210901732.s3.amazonaws.com/media/profile_images/...`
4. Image should load without 404 errors

## Troubleshooting

### Permission Denied
- Check IAM role has S3 permissions
- Verify bucket policy allows public read
- Check bucket name matches environment variable

### Files not uploading
- Check AWS credentials are set correctly
- Verify bucket exists and is accessible
- Check Django logs for S3 errors

### 404 errors
- Verify bucket policy allows public read
- Check file path in S3 matches Django's MEDIA_URL
- Ensure CORS is configured if accessing from different domain

## Cost Estimate

- **Storage**: ~$0.023 per GB/month
- **Requests**: ~$0.0004 per 1,000 GET requests
- **Data transfer**: First 100 GB free, then ~$0.09/GB

For a typical app with 1GB of images and 100K requests/month: **~$0.50/month**
