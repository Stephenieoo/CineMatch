# CloudFront Media Files Configuration Guide

## Problem
Profile images and other media files are returning 404 errors when accessed via CloudFront (`https://dz8ddbc7adyzz.cloudfront.net/media/...`). This happens because CloudFront doesn't serve `/media/` files by default.

## Solution
Configure CloudFront to serve `/media/*` paths by creating a cache behavior that forwards these requests to your Elastic Beanstalk origin.

## Steps to Configure CloudFront for Media Files

### 1. Go to CloudFront Console
- Navigate to AWS CloudFront → Distributions
- Select your distribution: `dz8ddbc7adyzz.cloudfront.net` (or find it by distribution ID)

### 2. Create Cache Behavior for `/media/*`

1. Click on your distribution
2. Go to the **"Behaviors"** tab
3. Click **"Create behavior"**

### 3. Configure the Behavior

Fill in the following settings:

#### Basic Settings:
- **Path pattern**: `/media/*`
- **Origin and origin groups**: Select your EB origin (`cinematch-production.eba-mixpvdvh.us-east-1.elasticbeanstalk.com`)

#### Viewer:
- **Viewer protocol policy**: `Redirect HTTP to HTTPS` or `HTTPS Only`
- **Allowed HTTP methods**: `GET, HEAD, OPTIONS`
- **Cache HTTP methods**: Check `GET` and `HEAD` (OPTIONS is optional)

#### Cache key and origin requests:
- **Cache policy**: Select **"CachingDisabled"** (recommended for user uploads) OR **"CachingOptimized"** if you want caching
- **Origin request policy**: Select **"CORS-S3Origin"** or **"AllViewer"** (to forward all headers)

#### Response headers policy:
- Leave as default or create a custom one if needed

#### Additional settings:
- **Compress objects automatically**: `Yes` (optional, saves bandwidth)
- **Smooth streaming**: `No`
- **Field-level encryption**: Leave as default
- **Enable real-time logs**: `No` (unless you want logging)

### 4. Important: Behavior Order

**CRITICAL**: The `/media/*` behavior must be placed **BEFORE** the default `*` behavior in the list.

CloudFront processes behaviors in order, and the first matching pattern wins. If the default `*` behavior comes first, it will catch all requests including `/media/*`.

To reorder:
1. In the Behaviors tab, you'll see a list of behaviors
2. Use the up/down arrows or drag to reorder
3. Ensure `/media/*` is above `*` (default)

### 5. Save and Deploy

1. Click **"Create behavior"**
2. CloudFront will show **"In Progress"** status
3. Wait for deployment to complete (usually 5-15 minutes)
4. You can check status in the CloudFront console

## Alternative: Modify Default Behavior

If you prefer to serve media files through the default behavior:

1. Edit the default behavior (`*`)
2. Ensure **Origin request policy** forwards necessary headers
3. Set **Cache policy** to **"CachingDisabled"** or a short TTL for `/media/*` paths
4. Save changes

However, creating a separate behavior for `/media/*` is recommended for better control.

## Verification

After deployment:

1. **Test Media File Access**:
   - Open your app: `https://dz8ddbc7adyzz.cloudfront.net`
   - Navigate to profile page
   - Check browser console for any 404 errors
   - Profile images should load without mixed content warnings

2. **Check Network Tab**:
   - Open browser DevTools → Network tab
   - Reload profile page
   - Look for requests to `/media/profile_images/...`
   - Verify they return `200 OK` instead of `404`

3. **Test Direct URL**:
   - Try accessing a media file directly: `https://dz8ddbc7adyzz.cloudfront.net/media/profile_images/your-image.jpg`
   - Should return the image, not a 404

## Code Changes

The code has been updated to:
- Use CloudFront domain for media files when accessed via CloudFront
- Use `https://` protocol to avoid mixed content warnings
- Fall back to EB domain only if CloudFront is not available

## Troubleshooting

### Still getting 404 errors
1. **Check behavior order** - `/media/*` must be before `*`
2. **Verify path pattern** - Should be exactly `/media/*`
3. **Check origin** - Ensure EB origin is correctly configured
4. **Wait for deployment** - CloudFront changes take 5-15 minutes to propagate

### Mixed content warnings
- Ensure you're using `https://` for CloudFront URLs
- Check that CloudFront is configured to redirect HTTP to HTTPS

### Images not updating
- If using caching, clear CloudFront cache or wait for TTL to expire
- Consider using "CachingDisabled" for user-uploaded content

## Additional Notes

- **Favicon 404**: This is a separate issue. Add a `favicon.ico` file to your Django static files or configure CloudFront to serve it.
- **Performance**: Using CloudFront for media files provides:
  - HTTPS support (no mixed content)
  - CDN caching (faster load times)
  - Better scalability

## Next Steps

1. Configure the `/media/*` behavior in CloudFront
2. Wait for deployment
3. Test profile page - images should load correctly
4. (Optional) Add favicon.ico to fix the favicon 404 error
