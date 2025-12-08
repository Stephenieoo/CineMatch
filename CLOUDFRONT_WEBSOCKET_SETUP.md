# CloudFront WebSocket Configuration Guide

## Problem
You cannot validate an ACM certificate for `*.elasticbeanstalk.com` because AWS owns that domain. However, CloudFront **DOES support WebSocket connections** and can proxy them to your EB origin.

## Solution
Configure CloudFront to support WebSocket connections and use CloudFront for WebSocket traffic instead of connecting directly to EB.

## Steps to Configure CloudFront for WebSockets

### 1. Go to CloudFront Console
- Navigate to AWS CloudFront → Distributions
- Select your distribution: `dz8ddbc7adyzz.cloudfront.net`

### 2. Edit Distribution Settings
- Click on your distribution ID
- Click **"Edit"** button

### 3. Configure Cache Behaviors for WebSocket
- Go to **"Behaviors"** tab
- You need to create a cache behavior for WebSocket paths (`/ws/*`)

#### Option A: Create a new behavior for `/ws/*`
1. Click **"Create behavior"**
2. **Path pattern**: `/ws/*`
3. **Origin**: Select your EB origin (`cinematch-production.eba-mixpvdvh.us-east-1.elasticbeanstalk.com`)
4. **Viewer protocol policy**: `Redirect HTTP to HTTPS` or `HTTPS Only`
5. **Allowed HTTP methods**: `GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE`
6. **Cache policy**: Select **"CachingDisabled"** (WebSockets need real-time connection)
7. **Origin request policy**: Select **"AllViewer"** or **"CORS-S3Origin"**
8. **Response headers policy**: Default or create one that allows WebSocket headers
9. **WebSocket**: **Enable** (this is the key setting!)
10. Click **"Create behavior"**

#### Option B: Modify default behavior
1. Select the default behavior (`*`)
2. Click **"Edit"**
3. Scroll to **"WebSocket"** section
4. **WebSocket**: Enable **"Yes"**
5. **Cache policy**: Consider using **"CachingDisabled"** for the default behavior if you want WebSockets to work everywhere
6. Save changes

### 4. Important Settings for WebSocket Support

#### Headers to Forward
Make sure these headers are forwarded to origin:
- `Upgrade`
- `Connection`
- `Sec-WebSocket-Key`
- `Sec-WebSocket-Version`
- `Sec-WebSocket-Protocol`
- `Origin`

#### Cache Settings
- **TTL**: Set to 0 or use "CachingDisabled" policy for `/ws/*` paths
- WebSocket connections are stateful and cannot be cached

### 5. Deploy Changes
- After making changes, CloudFront will show **"In Progress"** status
- Wait for deployment to complete (usually 5-15 minutes)
- You can check status in the CloudFront console

## Verification

After deployment:

1. **Test WebSocket Connection**:
   - Open your app via CloudFront: `https://dz8ddbc7adyzz.cloudfront.net`
   - Open browser console
   - Navigate to a group/community deck page
   - Check console logs for: `[WebSocket] Using CloudFront for WebSocket connection`
   - Verify connection succeeds without errors

2. **Check CloudFront Logs** (optional):
   - Enable CloudFront access logs
   - Look for WebSocket upgrade requests
   - Verify they're being proxied correctly

## Code Changes Made

The code has been updated to:
- Use CloudFront for WebSocket connections when accessed via CloudFront
- Use `wss://` protocol when page is HTTPS
- Fall back to direct EB connection for local development

## Troubleshooting

### WebSocket still fails
1. **Check CloudFront deployment status** - Wait for changes to propagate
2. **Verify WebSocket is enabled** in the behavior settings
3. **Check Origin settings** - Ensure EB origin is correctly configured
4. **Verify headers** - Make sure WebSocket headers are being forwarded
5. **Check browser console** - Look for specific error messages

### Connection timeout
- CloudFront has a default timeout of 60 seconds for WebSocket connections
- For long-lived connections, consider implementing reconnection logic (already in code)

### CORS issues
- Ensure CloudFront forwards the `Origin` header
- Check Django `ALLOWED_HOSTS` includes CloudFront domain
- Verify `CORS_ALLOWED_ORIGINS` in Django settings

## Alternative: Use Custom Domain

If you have a custom domain:
1. Request ACM certificate for your custom domain (e.g., `cinematch.com`)
2. Add custom domain to CloudFront distribution
3. Configure DNS to point to CloudFront
4. WebSocket will work through your custom domain

## Notes

- CloudFront WebSocket support is available in all regions
- There's no additional cost for WebSocket connections through CloudFront
- WebSocket connections count toward CloudFront data transfer charges
- CloudFront automatically handles WebSocket upgrade handshake
