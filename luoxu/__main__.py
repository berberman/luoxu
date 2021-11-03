import os
import re
import asyncio
import logging
import operator
from functools import partial
import importlib

from telethon import TelegramClient, events
from aiohttp import web

from .db import PostgreStore
from .group import GroupHistoryIndexer
from .util import load_config, UpdateLoaded
from . import web as myweb
from .ctxvars import msg_source

logger = logging.getLogger(__name__)

class Indexer:
  def __init__(self, config):
    self.config = config
    self.mark_as_read = config['telegram'].get('mark_as_read', True)
    self.dbstore = None
    self.msg_handlers = []

  def load_plugins(self, client):
    for plugin, conf in self.config.get('plugin', {}).items():
      if not conf.get('enabled', True):
        continue

      logger.info('loading plugin %s', plugin)
      mod = importlib.import_module(f'luoxu_plugins.{plugin}')
      mod.register(self, client)

  def add_msg_handler(self, handler, pattern='.*'):
    self.msg_handlers.append((handler, re.compile(pattern)))

  async def on_message(self, event):
    if isinstance(event, events.MessageEdited.Event):
      msg_source.set('editmsg')
    else:
      msg_source.set('newmsg')
    msg = event.message
    dbstore = self.dbstore

    if self.group_forward_history_done.get(msg.peer_id.channel_id, False):
      update_loaded = UpdateLoaded.update_last
    else:
      update_loaded = UpdateLoaded.update_none
    await dbstore.insert_messages([msg], update_loaded)

    if self.mark_as_read:
      try:
        await msg.mark_read(clear_mentions=True)
      except ConnectionError as e:
        logger.warning('cannot mark as read: %r', e)

    for handler, pattern in self.msg_handlers:
      logger.debug('message: %s, pattern: %s', msg.text, pattern)
      if pattern.fullmatch(msg.text):
        asyncio.create_task(handler(event))

  async def run(self):
    config = self.config
    tg_config = config['telegram']

    client = TelegramClient(
      tg_config['session_db'],
      tg_config['api_id'],
      tg_config['api_hash'],
      use_ipv6 = tg_config.get('ipv6', False),
      auto_reconnect = False, # we would miss updates between connections
    )
    if proxy := tg_config.get('proxy'):
      import socks
      client.set_proxy((socks.SOCKS5, proxy[0], int(proxy[1])))

    db = PostgreStore(config['database']['url'])
    await db.setup()
    self.dbstore = db

    web_config = config['web']
    cache_dir = web_config['cache_dir']
    os.makedirs(cache_dir, exist_ok=True)
    app = myweb.setup_app(
      db, client,
      os.path.abspath(cache_dir),
      os.path.abspath(web_config['default_avatar']),
      os.path.abspath(web_config['ghost_avatar']),
      prefix = web_config['prefix'],
      origins = web_config['origins'],
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
      runner,
      web_config['listen_host'], web_config['listen_port'],
    )
    await site.start()

    await client.start(tg_config['account'])
    index_group_ids = []
    group_entities = []
    for g in tg_config['index_groups']:
      if not g.startswith('@'):
        g = int(g)
      group = await client.get_entity(g)
      index_group_ids.append(group.id)
      group_entities.append(group)

    client.add_event_handler(self.on_message, events.NewMessage(chats=index_group_ids))
    client.add_event_handler(self.on_message, events.MessageEdited(chats=index_group_ids))

    self.load_plugins(client)

    try:
      while True:
        try:
          await self.run_on_connected(client, db, group_entities)
          logger.warning('disconnected, reconnecting in 1s')
          await asyncio.sleep(1)
        except ConnectionError:
          logger.exception('connection error, retry in 5s')
          await asyncio.sleep(5)
    finally:
      await runner.cleanup()

  async def run_on_connected(self, client, db, group_entities):
    self.group_forward_history_done = {}
    runnables = []
    for group in group_entities:
      ginfo = await self.init_group(group)
      gi = GroupHistoryIndexer(group, ginfo)
      runnables.append(gi.run(
        client, db,
        partial(operator.setitem, self.group_forward_history_done, group.id, True)
      ))

    if not client.is_connected():
      await client.start(self.config['telegram']['account'])
      # reset last ping to avoid reconnecting every 60s
      logger.info('resetting client._sender._ping')
      client._sender._ping = None

    # we do need to fetch history on startup because telethon doesn't
    # record group's pts in database.
    #
    # we also need to fetch history on reconnect because sometimes we still
    # don't see some missed updates (I don't know why).
    #
    # we may still miss edits that happen while we're offline and missed
    # the updates.
    gis = asyncio.gather(*runnables)
    await client.catch_up()
    try:
      await client.run_until_disconnected()
    finally:
      gis.cancel()
      try:
        await gis
      except asyncio.CancelledError:
        pass

  async def init_group(self, group):
    logger.info('init_group: %r', group.title)
    async with self.dbstore.get_conn() as conn:
      return await self.dbstore.insert_group(conn, group)

if __name__ == '__main__':
  from .lib.nicelogger import enable_pretty_logging
  # enable_pretty_logging('DEBUG')
  enable_pretty_logging('INFO')

  from .util import run_until_sigint

  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument('--config', default='config.toml',
                      help='config file path')
  args = parser.parse_args()

  config = load_config(args.config)
  indexer = Indexer(config)
  run_until_sigint(indexer.run())
