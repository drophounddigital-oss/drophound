"""DropHound — drop-tracking & resale-intelligence for blind-box collectors.

The package is organized to mirror the business plan:

  Layer 1 (free funnel)   -> web landing + public drops feed + broadcast alerts
  Layer 2 (premium)       -> filters, resale tracking, collection P/L, restock ETA
  Layer 3 (affiliate)     -> outbound /go redirects with affiliate tags

  Automation stack        -> drophound.engine.{monitors,resale,digest,alerts,pipeline}
"""

__version__ = "0.1.0"
