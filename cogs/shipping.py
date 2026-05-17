"""Feature C: Shipping calculator with cart-style dropdown UI.

Discord ActionRow constraint: max 5 rows. Layout:
  Row 1: Product Select
  Row 2: Quantity Select
  Row 3: [Add] [Remove last] [Clear]
  Row 4: Destination Select (region -> country toggling)
  Row 5: [Calculate] [Search] [Reset]
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.country_search import Country, CountryRegistry
from services.i18n import get_ui_lang, normalize_locale, t
from services.sheets_client import SheetsClient

log = logging.getLogger(__name__)

PRODUCTS_PATH = Path(__file__).parent.parent / "data" / "products.json"


@dataclass
class Product:
    id: str
    name_ja: str
    name_en: str
    emoji: str
    weight_g: int
    unit_ja: str
    unit_en: str

    def name(self, lang: str) -> str:
        return self.name_en if lang == "en" else self.name_ja

    def unit(self, lang: str) -> str:
        return self.unit_en if lang == "en" else self.unit_ja


def load_products() -> list[Product]:
    data = json.loads(PRODUCTS_PATH.read_text("utf-8"))
    return [Product(**p) for p in data["products"]]


@dataclass
class CartItem:
    product: Product
    qty: int

    @property
    def weight_g(self) -> int:
        return self.product.weight_g * self.qty


@dataclass
class CartState:
    items: list[CartItem] = field(default_factory=list)
    country: Optional[Country] = None
    region_view: Optional[str] = None  # currently shown region in country select
    current_product: Optional[Product] = None
    current_qty: Optional[int] = None
    lang: str = "ja"

    def add(self, product: Product, qty: int) -> None:
        for it in self.items:
            if it.product.id == product.id:
                it.qty += qty
                return
        self.items.append(CartItem(product=product, qty=qty))

    def remove_last(self) -> None:
        if self.items:
            self.items.pop()

    def clear(self) -> None:
        self.items.clear()

    @property
    def items_weight_g(self) -> int:
        return sum(i.weight_g for i in self.items)


# ===========================================================================
# UI components
# ===========================================================================


class ProductSelect(discord.ui.Select):
    def __init__(self, view_ref: "ShippingView", products: list[Product]) -> None:
        self._view = view_ref
        st = view_ref.state
        options = [
            discord.SelectOption(
                label=f"{p.emoji} {p.name(st.lang)}"[:100],
                description=f"{p.weight_g}g/{p.unit(st.lang) or 'each'}"[:100],
                value=p.id,
                default=(st.current_product is not None and st.current_product.id == p.id),
            )
            for p in products
        ]
        ph = (
            f"{st.current_product.emoji} {st.current_product.name(st.lang)}"
            if st.current_product else t("shipping.ph_select_product", st.lang)
        )
        super().__init__(
            placeholder=ph[:100],
            min_values=1, max_values=1, options=options, row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        pid = self.values[0]
        prod = next((p for p in self._view.products if p.id == pid), None)
        self._view.state.current_product = prod
        await self._view.refresh(interaction)


class QuantitySelect(discord.ui.Select):
    QTY_CHOICES = [1, 2, 3, 5, 10, 20, 50, 100]
    OTHER_VALUE = "__other__"

    def __init__(self, view_ref: "ShippingView") -> None:
        self._view = view_ref
        st = view_ref.state
        options = [
            discord.SelectOption(
                label=str(q),
                value=str(q),
                default=(st.current_qty == q),
            )
            for q in self.QTY_CHOICES
        ]
        options.append(discord.SelectOption(
            label=t("shipping.ph_other_qty", st.lang),
            value=self.OTHER_VALUE,
        ))
        ph = str(st.current_qty) if st.current_qty else t("shipping.ph_select_quantity", st.lang)
        super().__init__(
            placeholder=ph[:100],
            min_values=1, max_values=1, options=options, row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v = self.values[0]
        if v == self.OTHER_VALUE:
            await interaction.response.send_modal(QuantityModal(self._view))
            return
        self._view.state.current_qty = int(v)
        await self._view.refresh(interaction)


class QuantityModal(discord.ui.Modal):
    qty_field: discord.ui.TextInput

    def __init__(self, view_ref: "ShippingView") -> None:
        lang = view_ref.state.lang
        super().__init__(title=t("shipping.modal_qty_title", lang))
        self._view = view_ref
        self.qty_field = discord.ui.TextInput(
            label=t("shipping.modal_qty_label", lang),
            placeholder="1-999",
            required=True, max_length=3,
        )
        self.add_item(self.qty_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            n = int(self.qty_field.value)
            if not 1 <= n <= 999:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                t("shipping.err_invalid_qty", self._view.state.lang), ephemeral=True
            )
            return
        self._view.state.current_qty = n
        await self._view.refresh(interaction)


class DestinationSelect(discord.ui.Select):
    BACK_VALUE = "__back__"
    SEARCH_VALUE = "__search__"

    def __init__(self, view_ref: "ShippingView") -> None:
        self._view = view_ref
        st = view_ref.state
        lang = st.lang
        if st.region_view is None:
            # Region list
            opts = []
            for rid, r in view_ref.registry.all_regions().items():
                count = len([c for c in view_ref.registry.by_region(rid)])
                opts.append(discord.SelectOption(
                    label=f"{r.get('emoji','')} {r['name_ja' if lang=='ja' else 'name_en']}"[:100],
                    description=f"{count} countries"[:100],
                    value=rid,
                ))
            opts.append(discord.SelectOption(
                label=t("shipping.btn_search", lang), value=self.SEARCH_VALUE
            ))
            placeholder = (
                f"🔒 {st.country.display(lang)}"[:100] if st.country
                else t("shipping.ph_select_region", lang)
            )
        else:
            # Country list for selected region
            countries = view_ref.registry.by_region(st.region_view)[:24]
            current_iso = st.country.iso3 or st.country.name_en if st.country else None
            opts = []
            for c in countries:
                cval = c.iso3 or c.name_en
                opts.append(discord.SelectOption(
                    label=c.display(lang)[:100],
                    description=c.name_en[:100] if lang == "ja" else c.name_ja[:100],
                    value=cval,
                    default=(cval == current_iso),
                ))
            opts.append(discord.SelectOption(
                label=t("shipping.btn_back_region", lang), value=self.BACK_VALUE
            ))
            placeholder = (
                f"🔒 {st.country.display(lang)}"[:100] if st.country
                else t("shipping.ph_select_country", lang)
            )

        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, options=opts, row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v = self.values[0]
        if v == self.BACK_VALUE:
            self._view.state.region_view = None
            await self._view.refresh(interaction)
            return
        if v == self.SEARCH_VALUE:
            await interaction.response.send_modal(SearchModal(self._view))
            return
        if self._view.state.region_view is None:
            self._view.state.region_view = v
            await self._view.refresh(interaction)
            return
        # Country selected
        for c in self._view.registry.countries:
            if (c.iso3 or c.name_en) == v:
                self._view.state.country = c
                break
        await self._view.refresh(interaction)


class SearchModal(discord.ui.Modal):
    q: discord.ui.TextInput

    def __init__(self, view_ref: "ShippingView") -> None:
        lang = view_ref.state.lang
        super().__init__(title=t("shipping.modal_search_title", lang))
        self._view = view_ref
        self.q = discord.ui.TextInput(
            label=t("shipping.modal_search_label", lang),
            placeholder="USA / アメリカ / US",
            required=True, max_length=64,
        )
        self.add_item(self.q)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        results, stage = self._view.registry.search(self.q.value)
        lang = self._view.state.lang
        if not results:
            await interaction.response.send_message(
                f"{t('shipping.err_search_no_hit', lang)}: `{self.q.value}`",
                ephemeral=True,
            )
            return
        if len(results) == 1:
            self._view.state.country = results[0]
            self._view.state.region_view = results[0].region or None
            await self._view.refresh(interaction)
            return
        # Multiple results - show ephemeral chooser
        view = SearchChooserView(self._view, results)
        lines = [f"🔍 `{self.q.value}` → {len(results)} hits ({stage})"]
        await interaction.response.send_message("\n".join(lines), view=view, ephemeral=True)


class SearchChooserView(discord.ui.View):
    def __init__(self, parent: "ShippingView", results: list[Country]) -> None:
        super().__init__(timeout=120)
        self.parent = parent
        opts = [
            discord.SelectOption(
                label=c.display(parent.state.lang)[:100],
                value=c.iso3 or c.name_en,
            )
            for c in results[:25]
        ]
        sel = discord.ui.Select(placeholder=t("shipping.ph_select_country", parent.state.lang), options=opts)

        async def cb(interaction: discord.Interaction) -> None:
            v = sel.values[0]
            for c in results:
                if (c.iso3 or c.name_en) == v:
                    parent.state.country = c
                    parent.state.region_view = c.region
                    break
            await interaction.response.edit_message(content="✅ selected", view=None)
            # also update parent via a followup
            try:
                await parent.message.edit(**parent.render())
            except Exception:
                pass

        sel.callback = cb  # type: ignore[assignment]
        self.add_item(sel)


# ===========================================================================
# Action buttons
# ===========================================================================


class AddToCartButton(discord.ui.Button):
    """Submit button: requires product + qty + destination. Destination stays after submit."""
    def __init__(self, view_ref: "ShippingView") -> None:
        st = view_ref.state
        ready = bool(st.current_product and st.current_qty and st.country)
        super().__init__(
            style=discord.ButtonStyle.success,
            label=t("shipping.btn_add", view_ref.state.lang),
            row=2,
            disabled=not ready,
        )
        self._view = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        st = self._view.state
        if st.current_product and st.current_qty and st.country:
            st.add(st.current_product, st.current_qty)
            # Reset only product/qty. Destination (country/region_view) stays locked.
            st.current_product = None
            st.current_qty = None
        await self._view.refresh(interaction)


class RemoveLastButton(discord.ui.Button):
    def __init__(self, view_ref: "ShippingView") -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=t("shipping.btn_remove_last", view_ref.state.lang),
            row=2, disabled=not view_ref.state.items,
        )
        self._view = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        self._view.state.remove_last()
        await self._view.refresh(interaction)


class ClearButton(discord.ui.Button):
    def __init__(self, view_ref: "ShippingView") -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=t("shipping.btn_clear", view_ref.state.lang),
            row=2, disabled=not view_ref.state.items,
        )
        self._view = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        self._view.state.clear()
        await self._view.refresh(interaction)


class CalculateButton(discord.ui.Button):
    def __init__(self, view_ref: "ShippingView") -> None:
        ready = bool(view_ref.state.items and view_ref.state.country)
        super().__init__(
            style=discord.ButtonStyle.success,
            label=t("shipping.btn_calculate", view_ref.state.lang),
            row=4, disabled=not ready,
        )
        self._view = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException as e:
            # 40060 = another bot instance already ack'd this interaction.
            # Nothing we can do; surfacing the noise just confuses the user.
            log.warning("calculate: defer failed (%s)", e)
            return
        try:
            result_msg = await self._view.compute_and_render()
        except Exception:
            log.exception("calculate: compute_and_render raised")
            lang = self._view.state.lang
            await interaction.followup.send(
                f"❌ {t('shipping.err_no_match', lang)}",
                ephemeral=True,
            )
            return
        try:
            await interaction.followup.edit_message(
                message_id=self._view.message.id, **result_msg
            )
        except discord.HTTPException:
            log.exception("calculate: followup.edit_message failed")
            await interaction.followup.send(
                result_msg.get("content", "❌"), ephemeral=True
            )


class ResetButton(discord.ui.Button):
    def __init__(self, view_ref: "ShippingView") -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=t("shipping.btn_reset", view_ref.state.lang),
            row=4,
        )
        self._view = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        self._view.state = CartState(lang=self._view.state.lang)
        await self._view.refresh(interaction)


class LanguageToggleButton(discord.ui.Button):
    def __init__(self, view_ref: "ShippingView") -> None:
        next_lang = "en" if view_ref.state.lang == "ja" else "ja"
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"🌐 {next_lang.upper()}",
            row=4,
        )
        self._view = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        self._view.state.lang = "en" if self._view.state.lang == "ja" else "ja"
        await self._view.refresh(interaction)


# ===========================================================================
# Main view
# ===========================================================================


class ShippingView(discord.ui.View):
    message: discord.Message  # set by cog after sending

    def __init__(
        self,
        cog: "ShippingCog",
        registry: CountryRegistry,
        products: list[Product],
        sheets: SheetsClient,
        lang: str,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.registry = registry
        self.products = products
        self.sheets = sheets
        self.state = CartState(lang=lang)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(ProductSelect(self, self.products))
        self.add_item(QuantitySelect(self))
        self.add_item(AddToCartButton(self))
        self.add_item(RemoveLastButton(self))
        self.add_item(ClearButton(self))
        self.add_item(DestinationSelect(self))
        self.add_item(CalculateButton(self))
        self.add_item(ResetButton(self))
        self.add_item(LanguageToggleButton(self))

    def render(self) -> dict:
        return {
            "content": self._content(),
            "view": self,
        }

    def _content(self) -> str:
        lang = self.state.lang
        st = self.state
        lines = [f"**{t('shipping.panel_title', lang)}**"]
        lines.append(f"_{t('shipping.panel_subtitle', lang)}_")
        lines.append("")

        # Locked destination indicator (固定表示)
        if st.country:
            lines.append(f"🔒 **{t('shipping.destination', lang)}**: {st.country.display(lang)}")
        else:
            lines.append(f"⚠️ **{t('shipping.destination', lang)}**: _{t('shipping.ph_select_country', lang)}_")
        lines.append("")

        # Current selection (about to submit)
        prod_mark = "✅" if st.current_product else "⬜"
        qty_mark = "✅" if st.current_qty else "⬜"
        prod_text = (
            f"{st.current_product.emoji} {st.current_product.name(lang)}"
            if st.current_product else f"_{t('shipping.ph_select_product', lang)}_"
        )
        qty_text = str(st.current_qty) if st.current_qty else f"_{t('shipping.ph_select_quantity', lang)}_"
        lines.append(f"{prod_mark} 🎁 {prod_text}")
        lines.append(f"{qty_mark} 🔢 {qty_text}")

        # Cart contents
        if st.items:
            lines.append("")
            lines.append(f"**{t('shipping.cart_label', lang)}**")
            for it in st.items:
                lines.append(
                    f"・{it.product.emoji} {it.product.name(lang)} × {it.qty}{it.product.unit(lang)} = {it.weight_g:,}g"
                )
            items_g = st.items_weight_g
            pkg_g = int(os.getenv("PACKAGING_WEIGHT_G", "1000"))
            total_g = items_g + pkg_g
            lines.append(
                f"⚖ {items_g:,}g + 📦 {pkg_g:,}g = **{total_g:,}g** "
                f"({t('shipping.bracket', lang)}: "
                f"{SheetsClient.round_up_to_half(total_g/1000):.1f}kg)"
            )
        else:
            lines.append("")
            lines.append(f"_🛒 {t('shipping.cart_empty', lang)}_")

        return "\n".join(lines)[:1900]

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        if interaction.response.is_done():
            await interaction.followup.edit_message(
                message_id=self.message.id, **self.render()
            )
        else:
            await interaction.response.edit_message(**self.render())

    async def compute_and_render(self) -> dict:
        lang = self.state.lang
        st = self.state
        if not st.items:
            return {"content": t("shipping.err_cart_empty", lang), "view": self}
        if not st.country:
            return {"content": t("shipping.err_no_destination", lang), "view": self}
        if st.country.excluded:
            reason = st.country.reason_en if lang == "en" else st.country.reason_ja
            return {
                "content": f"{t('shipping.err_country_excluded', lang)}: {st.country.display(lang)}\n{reason}",
                "view": self,
            }

        block_header = self.registry.block_header(st.country)
        if not block_header:
            return {"content": f"❌ block not configured for {st.country.display(lang)}", "view": self}

        pkg_g = int(os.getenv("PACKAGING_WEIGHT_G", "1000"))
        max_box_kg = float(os.getenv("MAX_BOX_TOTAL_KG", "20"))
        items_g = st.items_weight_g
        per_box_net_g = int((max_box_kg * 1000) - pkg_g)  # 19000g

        if items_g + pkg_g <= max_box_kg * 1000:
            # single box
            total_g = items_g + pkg_g
            box_kg = SheetsClient.round_up_to_half(total_g / 1000)
            rate = self.sheets.lookup(block_header, box_kg)
            if rate is None:
                return {
                    "content": f"{t('shipping.err_no_match', lang)} ({block_header} / {box_kg}kg)",
                    "view": self,
                }
            return self._format_single(rate, items_g, pkg_g, total_g, box_kg)

        # split shipping
        n_boxes = math.ceil(items_g / per_box_net_g)
        base_net = items_g // n_boxes
        boxes_g = [base_net + pkg_g] * n_boxes
        boxes_g[-1] += items_g - base_net * n_boxes  # remainder to last box

        carriers: list[str] = []
        prices: list[int] = []
        brackets: list[float] = []
        for box_g in boxes_g:
            box_kg = SheetsClient.round_up_to_half(box_g / 1000)
            rate = self.sheets.lookup(block_header, box_kg)
            if rate is None:
                return {
                    "content": f"{t('shipping.err_weight_overflow', lang)} (box {box_kg}kg > sheet max)",
                    "view": self,
                }
            carriers.append(rate.carrier)
            prices.append(rate.price_jpy)
            brackets.append(box_kg)

        return self._format_split(boxes_g, brackets, carriers, prices, items_g, pkg_g)

    def _format_single(
        self, rate, items_g: int, pkg_g: int, total_g: int, bracket_kg: float
    ) -> dict:
        lang = self.state.lang
        st = self.state
        lines = [f"**{t('shipping.panel_result_title', lang)}**", "```"]
        lines.append(f"{t('shipping.items_label', lang)}:")
        for it in st.items:
            lines.append(
                f"  ・{it.product.name(lang)} × {it.qty}{it.product.unit(lang)} = {it.weight_g:,}g"
            )
        lines.append("─────────────────────────────")
        lines.append(f"  {t('shipping.items_subtotal', lang)} : {items_g:,}g")
        lines.append(f"  {t('shipping.packaging', lang)}      : {pkg_g:,}g")
        lines.append(f"  {t('shipping.total_weight', lang)}    : {total_g:,}g  ({t('shipping.bracket', lang)} {bracket_kg:.1f}kg)")
        lines.append("")
        lines.append(f"  {t('shipping.destination', lang)} : {st.country.display(lang)}")
        lines.append(f"  {t('shipping.carrier', lang)}      : {rate.carrier}  {t('shipping.carrier_note', lang)}")
        lines.append(f"  {t('shipping.price', lang)}         : ¥{rate.price_jpy:,}{t('shipping.currency_suffix', lang)}")
        lines.append("```")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M JST")
        lines.append(t("shipping.footer_source", lang, ts=ts))
        lines.append(t("shipping.footer_packaging", lang, kg=pkg_g/1000))
        ch = os.getenv("SHIPPING_GUIDE_CHANNEL_ID", "")
        if ch:
            lines.append(t("shipping.footer_disclaimer", lang, ch=ch))
        return {"content": "\n".join(lines)[:1900], "view": ResultActionView(self)}

    def _format_split(
        self,
        boxes_g: list[int],
        brackets: list[float],
        carriers: list[str],
        prices: list[int],
        items_g: int,
        pkg_g: int,
    ) -> dict:
        lang = self.state.lang
        st = self.state
        lines = [
            f"**{t('shipping.panel_result_title', lang)}**",
            t("shipping.split_shipping", lang, n=len(boxes_g)),
            "```",
        ]
        lines.append(f"{t('shipping.items_label', lang)}:")
        for it in st.items:
            lines.append(
                f"  ・{it.product.name(lang)} × {it.qty}{it.product.unit(lang)} = {it.weight_g:,}g"
            )
        lines.append("─────────────────────────────")
        for i, (g, kg, c, p) in enumerate(zip(boxes_g, brackets, carriers, prices), 1):
            lines.append(
                f"  {t('shipping.split_box', lang, i=i)}: {g:,}g ({kg:.1f}kg) → {c} ¥{p:,}"
            )
        lines.append("─────────────────────────────")
        lines.append(f"  {t('shipping.items_subtotal', lang)}   : {items_g:,}g")
        lines.append(f"  {t('shipping.packaging', lang)} ×{len(boxes_g)}: {pkg_g*len(boxes_g):,}g")
        lines.append("")
        lines.append(f"  {t('shipping.destination', lang)} : {st.country.display(lang)}")
        total_price = sum(prices)
        lines.append(f"  {t('shipping.price', lang)} (total): ¥{total_price:,}{t('shipping.currency_suffix', lang)}")
        lines.append("```")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M JST")
        lines.append(t("shipping.footer_source", lang, ts=ts))
        lines.append(t("shipping.footer_packaging", lang, kg=pkg_g/1000))
        ch = os.getenv("SHIPPING_GUIDE_CHANNEL_ID", "")
        if ch:
            lines.append(t("shipping.footer_disclaimer", lang, ch=ch))
        return {"content": "\n".join(lines)[:1900], "view": ResultActionView(self)}


class ResultActionView(discord.ui.View):
    def __init__(self, parent: ShippingView) -> None:
        super().__init__(timeout=600)
        self.parent = parent
        lang = parent.state.lang

        publish = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=t("shipping.btn_publish", lang),
        )
        async def on_publish(interaction: discord.Interaction) -> None:
            content = parent.message.content
            summary = t("shipping.publish_summary", lang, user=interaction.user.mention)
            try:
                await interaction.channel.send(f"{summary}\n{content}")
                await interaction.response.send_message("📤 published", ephemeral=True)
            except Exception:
                log.exception("publish failed")
                await interaction.response.send_message("⚠️ publish failed", ephemeral=True)
        publish.callback = on_publish  # type: ignore[assignment]

        redo = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=t("shipping.btn_redo", lang),
        )
        async def on_redo(interaction: discord.Interaction) -> None:
            parent.state = CartState(lang=parent.state.lang)
            parent._build()
            await interaction.response.edit_message(**parent.render())
        redo.callback = on_redo  # type: ignore[assignment]

        self.add_item(publish)
        self.add_item(redo)


# ===========================================================================
# Cog
# ===========================================================================


class ShippingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.registry = CountryRegistry()
        self.products = load_products()
        self.sheets = SheetsClient()

    @app_commands.command(name="shipping", description="Calculate shipping")
    async def shipping(self, interaction: discord.Interaction) -> None:
        from services.channel_guard import ensure_channel_allowed
        if not await ensure_channel_allowed(interaction, "shipping"):
            return
        lang = get_ui_lang(str(interaction.locale), feature="shipping")
        view = ShippingView(self, self.registry, self.products, self.sheets, lang=lang)
        await interaction.response.send_message(**view.render(), ephemeral=True)
        view.message = await interaction.original_response()

    shipping_admin_group = app_commands.Group(
        name="shippingadmin",
        description="送料BOTの管理コマンド",
        default_permissions=discord.Permissions(administrator=True),
    )

    @shipping_admin_group.command(name="reload", description="スプシと商品データを再読み込み")
    async def reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.registry.reload()
            self.products = load_products()
            data = self.sheets.reload()
            msg = (
                f"🔄 reloaded\n"
                f"・countries: `{len(self.registry.countries)}`\n"
                f"・products: `{len(self.products)}`\n"
                f"・sheet blocks: `{len(data.get('blocks', {}))}`\n"
                f"・max weight: `{data.get('max_kg', 0)}kg`"
            )
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            log.exception("reload failed")
            await interaction.followup.send(f"⚠️ reload failed: {e}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShippingCog(bot))
