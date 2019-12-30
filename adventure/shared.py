import asyncio
import discord
import time

locks = {}

def get_lock(member: discord.Member):
    if member.id not in locks:
        locks[member.id] = asyncio.Lock()
    return locks[member.id]

async def get_epoch(seconds: int):
    epoch = time.time()
    epoch += seconds
    return epoch

async def remaining(epoch):
    remaining = epoch - time.time()
    finish = remaining < 0
    m, s = divmod(remaining, 60)
    h, m = divmod(m, 60)
    s = int(s)
    m = int(m)
    h = int(h)
    if h == 0 and m == 0:
        out = "{:02d}".format(s)
    elif h == 0:
        out = "{:02d}:{:02d}".format(m, s)
    else:
        out = "{:01d}:{:02d}:{:02d}".format(h, m, s)
    return out, finish, remaining
