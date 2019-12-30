import asyncio
import contextlib
import json
import logging
import os
import random
import time
from collections import namedtuple
from datetime import date, datetime
from types import SimpleNamespace
from typing import List, Optional, Union

import discord
from redbot.cogs.bank import check_global_setting_admin
from redbot.core import Config, bank, checks, commands
from redbot.core.commands import Context
from redbot.core.data_manager import bundled_data_path, cog_data_path
from redbot.core.errors import BalanceTooHigh
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import (
    bold,
    box,
    escape,
    humanize_list,
    humanize_timedelta,
    pagify,
)
from redbot.core.utils.common_filters import filter_various_mentions
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu, start_adding_reactions
from redbot.core.utils.predicates import MessagePredicate, ReactionPredicate

import adventure.charsheet
from .charsheet import (
    Character,
    GameSession,
    Item,
    ItemConverter,
    Stats,
    calculate_sp,
    can_equip,
    equip_level,
    has_funds,
    parse_timedelta,
)

import adventure.shared
from .shared import (
    get_lock,
    get_epoch,
    remaining,
)

try:
    from redbot.core.utils.chat_formatting import humanize_number
except ImportError:
    def humanize_number(val: int) -> str:
        return "{:,}".format(val)

log = logging.getLogger("red.cogs.adventure")
_ = Translator("Adventure", __file__)

TR_GEAR_SET = {}
TR_LEGENDARY = {}
TR_EPIC = {}
TR_RARE = {}
TR_COMMON = {}

class Cart:
    def __init__(self, bot):
        self.bot = bot
        self.tasks = {}
        self.config = Config.get_conf(self, 2_710_801_001, force_registration=True)
        default_guild = {
            "cart_channels": [],
            "cart_name": "",
            "cooldown": 0,
            "cartroom": None,
            "cart_timeout": 10800,
        }
        default_global = {
            "cart_name": _("Hawl's brother"),
            "enable_chests": True,
        }
        self.config.register_guild(**default_guild)
        self.config.register_global(**default_global)

        self._last_trade = {}
        self._trader_countdown = {}
        self._current_traders = {}
        self._curent_trader_stock = {}

    async def handle_cart(self, reaction, user):
        log.debug("handling cart")
        log.debug(self.config)
        log.debug(await self.config.user(user).all())
        guild = user.guild
        emojis = ReactionPredicate.NUMBER_EMOJIS
        itemindex = emojis.index(str(reaction.emoji)) - 1
        items = self._current_traders[guild.id]["stock"][itemindex]
        self._current_traders[guild.id]["users"].append(user)
        spender = user
        channel = reaction.message.channel
        currency_name = await bank.get_currency_name(guild)
        if currency_name.startswith("<"):
            currency_name = "credits"
        item_data = box(items["itemname"] + " - " + humanize_number(items["price"]), lang="css")
        to_delete = await channel.send(
            _("{user}, how many {item} would you like to buy?").format(
                user=user.mention, item=item_data
            )
        )
        ctx = await self.bot.get_context(reaction.message)
        ctx.author = user
        pred = MessagePredicate.valid_int(ctx)
        try:
            msg = await self.bot.wait_for("message", check=pred, timeout=30)
        except asyncio.TimeoutError:
            self._current_traders[guild.id]["users"].remove(user)
            return
        if pred.result < 1:
            with contextlib.suppress(discord.HTTPException):
                await to_delete.delete()
                await msg.delete()
            await smart_embed(ctx, _("You're wasting my time."))
            self._current_traders[guild.id]["users"].remove(user)
            return
        if await bank.can_spend(spender, int(items["price"]) * pred.result):
            await bank.withdraw_credits(spender, int(items["price"]) * pred.result)
            async with get_lock(user):
                try:
                    log.debug(self.config)
                    log.debug(await self.config.user(user).all())
                    #  TODO: make shared config? # 
                    c = await Character.from_json(self.config, user)
                except Exception:
                    log.exception("Error with the new character sheet")
                    return
                if "chest" in items["itemname"]:
                    if items["itemname"] == ".rare_chest":
                        c.treasure[1] += pred.result
                    elif items["itemname"] == "[epic chest]":
                        c.treasure[2] += pred.result
                    else:
                        c.treasure[0] += pred.result
                else:
                    item = items["item"]
                    item.owned = pred.result
                    if item.name in c.backpack:
                        c.backpack[item.name].owned += pred.result
                    else:
                        c.backpack[item.name] = item
                await self.config.user(user).set(c.to_json())
                with contextlib.suppress(discord.HTTPException):
                    await to_delete.delete()
                    await msg.delete()
                await channel.send(
                    box(
                        _(
                            "{author} bought {p_result} {item_name} for "
                            "{item_price} {currency_name} and put it into their backpack."
                        ).format(
                            author=self.escape(user.display_name),
                            p_result=pred.result,
                            item_name=items["itemname"],
                            item_price=humanize_number(items["price"] * pred.result),
                            currency_name=currency_name,
                        ),
                        lang="css",
                    )
                )
                self._current_traders[guild.id]["users"].remove(user)
        else:
            with contextlib.suppress(discord.HTTPException):
                await to_delete.delete()
                await msg.delete()
            await channel.smart_embed(
                _("{author}, you do not have enough {currency_name}.").format(
                    author=self.escape(user.display_name), currency_name=currency_name
                )
            )
            self._current_traders[guild.id]["users"].remove(user)

    async def _cart_countdown(self, ctx: Context, seconds, title, room=None) -> asyncio.Task:
        await self._data_check(ctx)

        async def cart_countdown():
            secondint = int(seconds)
            cart_end = await get_epoch(secondint)
            timer, done, sremain = await remaining(cart_end)
            message_cart = await ctx.send(f"⏳ [{title}] {timer}s")
            while not done:
                timer, done, sremain = await remaining(cart_end)
                self._trader_countdown[ctx.guild.id] = (timer, done, sremain)
                if done:
                    await message_cart.delete()
                    break
                if int(sremain) % 5 == 0:
                    await message_cart.edit(content=(f"⏳ [{title}] {timer}s"))
                await asyncio.sleep(1)

        return ctx.bot.loop.create_task(cart_countdown())

    async def _data_check(self, ctx: Context):
        try:
            self._trader_countdown[ctx.guild.id]
        except KeyError:
            self._trader_countdown[ctx.guild.id] = 0

    async def trader(self, ctx: Context, bypass=False):
        em_list = ReactionPredicate.NUMBER_EMOJIS

        cart = await self.config.cart_name()
        if await self.config.guild(ctx.guild).cart_name():
            cart = await self.config.guild(ctx.guild).cart_name()
        text = box(_("[{} is bringing the cart around!]").format(cart), lang="css")
        timeout = await self.config.guild(ctx.guild).cart_timeout()
        if ctx.guild.id not in self._last_trade:
            self._last_trade[ctx.guild.id] = 0

        if not bypass:
            if self._last_trade[ctx.guild.id] == 0:
                self._last_trade[ctx.guild.id] = time.time()
            elif self._last_trade[ctx.guild.id] >= time.time() - timeout:
                # trader can return after 3 hours have passed since last visit.
                return  # silent return.
        self._last_trade[ctx.guild.id] = time.time()

        room = await self.config.guild(ctx.guild).cartroom()
        if room:
            room = ctx.guild.get_channel(room)
        if room is None:
            room = ctx

        self.bot.dispatch("adventure_cart", ctx)  # dispatch after silent return

        #  stockcount = random.randint(3, 9)
        stockcount = 2
        controls = {em_list[i + 1]: i for i in range(stockcount)}
        self._curent_trader_stock[ctx.guild.id] = stockcount, controls

        stock = await self._trader_get_items(stockcount)
        currency_name = await bank.get_currency_name(ctx.guild)
        if str(currency_name).startswith("<"):
            currency_name = "credits"
        for index, item in enumerate(stock):
            item = stock[index]
            if "chest" not in item["itemname"]:
                if len(item["item"].slot) == 2:  # two handed weapons add their bonuses twice
                    hand = "two handed"
                    att = item["item"].att * 2
                    cha = item["item"].cha * 2
                    intel = item["item"].int * 2
                    luck = item["item"].luck * 2
                    dex = item["item"].dex * 2
                else:
                    if item["item"].slot[0] == "right" or item["item"].slot[0] == "left":
                        hand = item["item"].slot[0] + _(" handed")
                    else:
                        hand = item["item"].slot[0] + _(" slot")
                    att = item["item"].att
                    cha = item["item"].cha
                    intel = item["item"].int
                    luck = item["item"].luck
                    dex = item["item"].dex
                text += box(
                    _(
                        "\n[{i}] Lvl req {lvl} | {item_name} ("
                        "Attack: {str_att}, "
                        "Intelligence: {str_int}, "
                        "Charisma: {str_cha} "
                        "Luck: {str_luck} "
                        "Dexterity: {str_dex} "
                        "[{hand}]) for {item_price} {currency_name}."
                    ).format(
                        i=str(index + 1),
                        item_name=item["itemname"],
                        lvl=item["item"].lvl,
                        str_att=str(att),
                        str_int=str(intel),
                        str_cha=str(cha),
                        str_luck=str(luck),
                        str_dex=str(dex),
                        hand=hand,
                        item_price=humanize_number(item["price"]),
                        currency_name=currency_name,
                    ),
                    lang="css",
                )
            else:
                text += box(
                    _("\n[{i}] {item_name} " "for {item_price} {currency_name}.").format(
                        i=str(index + 1),
                        item_name=item["itemname"],
                        item_price=humanize_number(item["price"]),
                        currency_name=currency_name,
                    ),
                    lang="css",
                )
        text += _("Do you want to buy any of these fine items? Tell me which one below:")
        msg = await ctx.send(text)
        start_adding_reactions(msg, controls.keys())
        self._current_traders[ctx.guild.id] = {"msg": msg.id, "stock": stock, "users": []}
        timeout = self._last_trade[ctx.guild.id] + 180 - time.time()
        if timeout <= 0:
            timeout = 0
        timer = await self._cart_countdown(ctx, timeout, _("The cart will leave in: "))
        self.tasks[msg.id] = timer
        try:
            await asyncio.wait_for(timer, timeout + 5)
        except asyncio.TimeoutError:
            await self._clear_react(msg)
            return
        with contextlib.suppress(discord.HTTPException):
            await msg.delete()

    async def _trader_get_items(self, howmany: int):
        items = {}
        output = {}

        #  chest_type = random.randint(1, 100)
        chest_type = 61
        chest_enable = await self.config.enable_chests()
        while len(items) < howmany:
            chance = None
            #  roll = random.randint(1, 100)
            roll = 5
            if chest_type <= 60:
                if roll <= 5:
                    chance = TR_EPIC
                elif 5 < roll <= 25:
                    chance = TR_RARE
                elif roll >= 90 and chest_enable:
                    chest = [1, 0, 0]
                    types = ["normal chest", ".rare_chest", "[epic chest]"]
                    if "normal chest" not in items:
                        items.update(
                            {
                                "normal chest": {
                                    "itemname": _("normal chest"),
                                    "item": chest,
                                    "price": 100000,
                                }
                            }
                        )
                else:
                    chance = TR_COMMON
            elif chest_type <= 75:
                if roll <= 15:
                    chance = TR_EPIC
                elif 15 < roll <= 45:
                    chance = TR_RARE
                elif roll >= 90 and chest_enable:
                    chest = random.choice([[0, 1, 0], [1, 0, 0]])
                    types = ["normal chest", ".rare_chest", "[epic chest]"]
                    prices = [10000, 50000, 100000]
                    chesttext = types[chest.index(1)]
                    price = prices[chest.index(1)]
                    if chesttext not in items:
                        items.update(
                            {
                                chesttext: {
                                    "itemname": "{}".format(chesttext),
                                    "item": chest,
                                    "price": price,
                                }
                            }
                        )
                else:
                    chance = TR_COMMON
            else:
                if roll <= 25:
                    chance = TR_EPIC
                elif roll >= 90 and chest_enable:
                    chest = random.choice([[0, 1, 0], [0, 0, 1]])
                    types = ["normal chest", ".rare_chest", "[epic chest]"]
                    prices = [10000, 50000, 100000]
                    chesttext = types[chest.index(1)]
                    price = prices[chest.index(1)]
                    if chesttext not in items:
                        items.update(
                            {
                                chesttext: {
                                    "itemname": "{}".format(chesttext),
                                    "item": chest,
                                    "price": price,
                                }
                            }
                        )
                else:
                    chance = TR_RARE

            if chance is not None:
                itemname = random.choice(list(chance.keys()))
                item = Item.from_json({itemname: chance[itemname]})
                if len(item.slot) == 2:  # two handed weapons add their bonuses twice
                    att = item.att * 2
                    cha = item.cha * 2
                    intel = item.int * 2
                else:
                    att = item.att
                    cha = item.cha
                    intel = item.int
                if item.rarity == "epic":
                    #  price = random.randint(10000, 50000) * max(att + cha + intel, 1)
                    #  price = random.randint(3000, 6000) * max(att + cha + intel, 1)
                    price = random.randint(2000, 5000) * max(att + cha + intel, 1)
                elif item.rarity == "rare":
                    #  price = random.randint(2000, 5000) * max(att + cha + intel, 1)
                    #  price = random.randint(500, 2000) * max(att + cha + intel, 1)
                    price = random.randint(250, 500) * max(att + cha + intel, 1)
                else:
                    #  price = random.randint(100, 250) * max(att + cha + intel, 1)
                    #  price = random.randint(200, 400) * max(att + cha + intel, 1)
                    price = random.randint(50, 100) * max(att + cha + intel, 1)
                if itemname not in items:
                    items.update(
                        {
                            itemname: {
                                "itemname": itemname,
                                "item": item,
                                "price": price,
                                "lvl": item.lvl,
                            }
                        }
                    )

        for index, item in enumerate(items):
            output.update({index: items[item]})
        return output

    async def cleanup_tasks(self):
        to_delete = []
        for msg_id, task in self.tasks.items():
            if task.done():
                to_delete.append(msg_id)
        for task in to_delete:
            del self.tasks[task]

    def valid_react_to_trade(self, reaction, user, guild):
        """TODO: Docstring for valid_react_to_trade.

        :reaction: TODO
        :user: TODO
        :guild: TODO
        :returns: TODO

        """
        #  log.debug(self._current_traders)
        guild_currently_trading = guild.id in self._current_traders
        reacted_to_trade_msg = reaction.message.id == self._current_traders[guild.id]["msg"]
        user_not_cur_trading = user not in self._current_traders[guild.id]["users"]
        valid_react = guild_currently_trading and reacted_to_trade_msg and user_not_cur_trading
        return guild_currently_trading and reacted_to_trade_msg and user_not_cur_trading
