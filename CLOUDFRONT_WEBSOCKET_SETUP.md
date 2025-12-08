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

#### Option A: Create a new behavior for `/ws/*` (Recommended)
1. Click **"Create behavior"**
2. **Path pattern**: `/ws/*`
3. **Origin**: Select your EB origin (`cinematch-production.eba-mixpvdvh.us-east-1.elasticbeanstalk.com`)
4. **Viewer protocol policy**: `Redirect HTTP to HTTPS` or `HTTPS Only`
5. **Allowed HTTP methods**: `GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE`
6. **Cache policy**: Select **"CachingDisabled"** (CRITICAL - WebSockets need real-time connection, cannot be cached)
7. **Origin request policy**: Select **"AllViewer"** (CRITICAL - This forwards all headers including WebSocket upgrade headers like `Upgrade`, `Connection`, `Sec-WebSocket-Key`, etc.)
8. **Response headers policy**: Default or create one if needed
9. Click **"Create behavior"**

**Note**: CloudFront doesn't have an explicit "WebSocket" toggle. WebSocket support is **automatic** when:
- Cache policy is set to "CachingDisabled" (so WebSocket upgrade requests aren't cached)
- Origin request policy forwards all headers (so WebSocket headers reach the origin)

#### Option B: Modify default behavior
1. Select the default behavior (`*`)
2. Click **"Edit"**
3. **Cache policy**: Change to **"CachingDisabled"** (if you want WebSockets to work everywhere)
4. **Origin request policy**: Ensure it's set to **"AllViewer"** (to forward WebSocket headers)
5. Save changes

**Note**: Modifying the default behavior affects all paths. If you only need WebSockets for `/ws/*`, Option A is better.

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
1. **Check CloudFront deployment status** - Wait for changes to propagate (5-15 minutes)
2. **Verify Cache Policy** - Must be "CachingDisabled" for `/ws/*` paths
3. **Verify Origin Request Policy** - Must be "AllViewer" to forward WebSocket headers
4. **Check behavior order** - `/ws/*` behavior must be **before** the default `*` behavior
5. **Check Origin settings** - Ensure EB origin is correctly configured
6. **Check browser console** - Look for specific error messages (connection refused, timeout, etc.)
7. **Test direct EB connection** - Try connecting directly to EB to verify WebSocket works there

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

## Important Notes

- **CloudFront doesn't have an explicit "WebSocket" toggle** - Support is automatic when configured correctly
- **Key settings for WebSocket support**:
  - Cache policy: `CachingDisabled` (prevents caching of WebSocket upgrade requests)
  - Origin request policy: `AllViewer` (forwards all headers including WebSocket upgrade headers)
- **Behavior order matters**: `/ws/*` behavior must be placed **before** the default `*` behavior
- CloudFront WebSocket support is available in all regions
- There's no additional cost for WebSocket connections through CloudFront
- WebSocket connections count toward CloudFront data transfer charges
- CloudFront automatically handles WebSocket upgrade handshake when headers are forwarded correctly
