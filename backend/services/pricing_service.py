"""
Spheron GPU Pricing Service - fetches current GPU pricing from the Spheron API.

Per-GPU pricing logic:
  - Offer HAS spot_price  -> spot instance  -> per_gpu = spot_price  / gpu_count
  - Offer has NO spot_price -> on-demand     -> per_gpu = price       / gpu_count
"""
import httpx
import logging

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
        Get per-GPU pricing summary keyed by GPU base type.

        For each individual offer inside a GPU entry:
          - If the offer has a truthy `spot_price` field -> it is a spot instance.
            per_gpu_price = spot_price / gpu_count
          - Otherwise -> it is an on-demand instance.
            per_gpu_price = price / gpu_count

        Returns the lowest per-GPU price seen across all providers for each type:
        {
            "H100": {"ondemand": 2.01, "spot": 0.99, "display_name": "H100 SXM5"},
            ...
        }
        """
        offers = await self.fetch_all_gpu_offers()
        pricing: dict[str, dict] = {}

        for gpu in offers:
            base_type = gpu.get("baseGpuType") or gpu.get("gpuType") or ""
            if not base_type:
                continue
            display_name = gpu.get("displayName", base_type)
            gpu_offers = gpu.get("offers", [])

            ondemand_prices: list[float] = []
            spot_prices: list[float] = []

            for offer in gpu_offers:
                # Support both snake_case and camelCase field names
                gpu_count = (
                    offer.get("gpu_count")
                    or offer.get("gpuCount")
                    or offer.get("numGpus")
                    or 1
                )
                try:
                    gpu_count = float(gpu_count)
                except (TypeError, ValueError):
                    gpu_count = 1.0
                if gpu_count <= 0:
                    gpu_count = 1.0

                spot_price = offer.get("spot_price")
                if spot_price:
                    # Spot instance - divide spot_price by gpu_count
                    try:
                        per_gpu = float(spot_price) / gpu_count
                        spot_prices.append(per_gpu)
                    except (TypeError, ValueError):
                        pass
                else:
                    # On-demand instance - divide price by gpu_count
                    price = offer.get("price", 0)
                    if price:
                        try:
                            per_gpu = float(price) / gpu_count
                            ondemand_prices.append(per_gpu)
                        except (TypeError, ValueError):
                            pass

            ondemand = round(min(ondemand_prices), 4) if ondemand_prices else None
            spot = round(min(spot_prices), 4) if spot_prices else None

            if ondemand is None and spot is None:
                continue

            pricing[base_type] = {
                "ondemand": ondemand,
                "spot": spot,
                "display_name": display_name,
            }

        return pricing

    def format_pricing_from_summary(self, pricing: dict, is_snapshot: bool = False) -> str:
        """
        Format a pre-fetched pricing summary dict as a markdown table for Claude prompts.
        Use this when you already have the result of get_pricing_summary() to avoid a
        second API call.
        is_snapshot=True changes the header to reflect that this is a stored snapshot,
        not a live fetch - used on review iterations 2+ to avoid contradicting the
        surrounding pricing_context instruction.
        """
        if not pricing:
            return "Pricing data unavailable - skip pricing verification this iteration."

        header = (
            "Spheron GPU **per-GPU** pricing snapshot ($/hr) captured at first review - use this, do not re-fetch:"
            if is_snapshot else
            "Current Spheron GPU **per-GPU** pricing ($/hr) fetched live from the Spheron API:"
        )
        lines = [
            header,
            "",
            "| GPU Model | On-Demand $/hr (per GPU) | Spot $/hr (per GPU) |",
            "|-----------|--------------------------|---------------------|",
        ]
        sorted_gpus = sorted(
            pricing.items(),
            key=lambda x: x[1].get("ondemand") or 0,
            reverse=True,
        )
        for gpu_type, info in sorted_gpus:
            ondemand = f"${info['ondemand']:.2f}" if info.get("ondemand") is not None else "N/A"
            spot     = f"${info['spot']:.2f}"     if info.get("spot")     is not None else "N/A"
            lines.append(f"| {info['display_name']} ({gpu_type}) | {ondemand} | {spot} |")

        lines += [
            "",
            "Note: prices fluctuate with GPU availability. Always include this disclaimer in the blog.",
        ]
        return "\n".join(lines)

    async def format_pricing_for_prompt(self) -> str:
        """
        Format per-GPU pricing as a markdown table ready to inject into Claude prompts.
        Returns a table the agent can use to verify pricing claims in the blog.
        """
        try:
            pricing = await self.get_pricing_summary()
        except Exception as e:
            logger.warning(f"Could not fetch pricing for prompt: {e}")
            return "Pricing data unavailable - skip pricing verification this iteration."

        return self.format_pricing_from_summary(pricing)

    async def format_pricing_for_comment(self) -> str:
        """Format per-GPU pricing for use in PR comments."""
        pricing = await self.get_pricing_summary()
        lines = [
            "**Current Spheron GPU Pricing** (per GPU/hr, prices fluctuate based on GPU availability):\n",
            "| GPU | On-Demand ($/hr per GPU) | Spot ($/hr per GPU) |",
            "|-----|--------------------------|---------------------|",
        ]
        sorted_gpus = sorted(
            pricing.items(),
            key=lambda x: x[1].get("ondemand") or 0,
            reverse=True,
        )
        for gpu_type, info in sorted_gpus:
            ondemand = f"${info['ondemand']:.2f}" if info.get("ondemand") is not None else "N/A"
            spot     = f"${info['spot']:.2f}"     if info.get("spot")     is not None else "N/A"
            lines.append(f"| {info['display_name']} | {ondemand} | {spot} |")

        return "\n".join(lines)
