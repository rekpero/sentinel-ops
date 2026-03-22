"""
Spheron GPU Pricing Service - fetches current GPU pricing from the Spheron API.
Supports pagination to get all GPU types.
"""
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PricingService:
    def __init__(self, api_url: str = "https://app.spheron.ai/api/gpu-offers"):
        self.api_url = api_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self.client.aclose()

    async def fetch_all_gpu_offers(self) -> list[dict]:
        """Fetch all GPU offers across all pages."""
        all_offers = []
        page = 1
        while True:
            resp = await self.client.get(
                self.api_url,
                params={"page": page, "limit": 50},
            )
            resp.raise_for_status()
            data = resp.json()
            offers = data.get("data", [])
            if not offers:
                break
            all_offers.extend(offers)
            if page >= data.get("totalPages", 1):
                break
            page += 1
        return all_offers

    async def get_pricing_summary(self) -> dict:
        """
        Get a clean pricing summary keyed by GPU base type.
        Returns: {
            "H100": {"ondemand": 2.01, "spot": 0.99, "display_name": "H100 SXM5"},
            "B200": {"ondemand": 6.03, "spot": 2.25, "display_name": "B200 SXM"},
            ...
        }
        """
        offers = await self.fetch_all_gpu_offers()
        pricing = {}

        for gpu in offers:
            base_type = gpu.get("baseGpuType", gpu.get("gpuType", "unknown"))
            display_name = gpu.get("displayName", base_type)
            gpu_offers = gpu.get("offers", [])

            ondemand_prices = []
            spot_prices = []

            for offer in gpu_offers:
                instance_type = offer.get("instanceType", "DEDICATED")
                price = offer.get("price", 0)
                if instance_type == "SPOT":
                    spot_prices.append(price)
                else:
                    ondemand_prices.append(price)

            # Use the lowest available price for each type
            lowest_ondemand = gpu.get("lowestPrice", 0)
            if not lowest_ondemand and ondemand_prices:
                lowest_ondemand = min(ondemand_prices)

            lowest_spot = min(spot_prices) if spot_prices else None

            # Also check averagePrice as fallback
            if not lowest_ondemand:
                lowest_ondemand = gpu.get("averagePrice", 0)

            pricing[base_type] = {
                "ondemand": round(lowest_ondemand, 2) if lowest_ondemand else None,
                "spot": round(lowest_spot, 2) if lowest_spot else None,
                "display_name": display_name,
                "total_available": gpu.get("totalAvailable", 0),
            }

        return pricing

    async def format_pricing_for_comment(self) -> str:
        """Format pricing data for use in PR comments."""
        pricing = await self.get_pricing_summary()
        lines = [
            "**Current Spheron GPU Pricing** (as of today, prices fluctuate based on GPU availability):\n",
            "| GPU | On-Demand ($/hr) | Spot ($/hr) |",
            "|-----|-------------------|-------------|",
        ]
        # Sort by ondemand price descending
        sorted_gpus = sorted(
            pricing.items(),
            key=lambda x: x[1].get("ondemand") or 0,
            reverse=True,
        )
        for gpu_type, info in sorted_gpus:
            ondemand = f"${info['ondemand']:.2f}" if info.get("ondemand") else "N/A"
            spot = f"${info['spot']:.2f}" if info.get("spot") else "N/A"
            lines.append(f"| {info['display_name']} | {ondemand} | {spot} |")

        return "\n".join(lines)

    async def check_blog_pricing(self, blog_content: str) -> list[dict]:
        """
        Check if any GPU pricing mentioned in the blog matches current API pricing.
        Returns list of mismatches with corrections.
        """
        import re

        pricing = await self.get_pricing_summary()
        mismatches = []

        for gpu_type, info in pricing.items():
            # Look for mentions of this GPU type with pricing
            # Match patterns like "$2.01/hr", "$2.01 per hour", "$2.01/hour"
            gpu_pattern = re.compile(
                rf"{re.escape(gpu_type)}[^$]*?\$(\d+\.?\d*)\s*(?:/hr|/hour|per hour)",
                re.IGNORECASE,
            )
            matches = gpu_pattern.findall(blog_content)

            for mentioned_price in matches:
                mentioned = float(mentioned_price)
                if info.get("ondemand") and abs(mentioned - info["ondemand"]) > 0.01:
                    mismatches.append({
                        "gpu": gpu_type,
                        "display_name": info["display_name"],
                        "mentioned_price": mentioned,
                        "current_ondemand": info["ondemand"],
                        "current_spot": info.get("spot"),
                    })

            # Also check spot pricing mentions
            spot_pattern = re.compile(
                rf"{re.escape(gpu_type)}[^$]*?spot[^$]*?\$(\d+\.?\d*)",
                re.IGNORECASE,
            )
            spot_matches = spot_pattern.findall(blog_content)
            for mentioned_price in spot_matches:
                mentioned = float(mentioned_price)
                if info.get("spot") and abs(mentioned - info["spot"]) > 0.01:
                    mismatches.append({
                        "gpu": gpu_type,
                        "display_name": info["display_name"],
                        "mentioned_price": mentioned,
                        "current_spot": info["spot"],
                        "type": "spot",
                    })

        return mismatches
