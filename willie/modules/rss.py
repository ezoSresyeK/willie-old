# coding=utf8
"""
rss.py - Willie RSS Module
Copyright 2012, Michael Yanovich, yanovich.net
Licensed under the Eiffel Forum License 2.

http://willie.dfbta.net
"""
from __future__ import unicode_literals

from datetime import datetime
import time
import re
import socket

import feedparser

from willie.module import commands, interval
from willie.config import ConfigurationError


socket.setdefaulttimeout(10)

INTERVAL = 60 * 30  # seconds between checking for new updates


def setup(bot):
    bot.memory['rss_manager'] = RSSManager(bot)

    if not bot.db:
        raise ConfigurationError("Database not set up, or unavailable.")
    conn = bot.db.connect()
    c = conn.cursor()

    # if new table doesn't exist, create it and try importing from old tables
    # The rss_feeds table was added on 2013-07-17.
    try:
        c.execute('SELECT * FROM rss_feeds')
    except StandardError:
        create_table(bot, c)
        migrate_from_old_tables(bot, c)

        # These tables are no longer used, but lets not delete them right away.
        # c.execute('DROP TABLE IF EXISTS rss')
        # c.execute('DROP TABLE IF EXISTS recent')

        conn.commit()

    # The modified column was added on 2013-07-21.
    try:
        c.execute('SELECT modified FROM rss_feeds')
    except StandardError:
        c.execute('ALTER TABLE rss_feeds ADD modified TEXT')
        conn.commit()

    conn.close()


def create_table(bot, c):
    # MySQL needs to only compare on the first n characters of a TEXT field
    # but SQLite won't accept the syntax needed to make it do it.
    if bot.db.type == 'mysql':
        primary_key = '(channel(254), feed_name(254))'
    else:
        primary_key = '(channel, feed_name)'

    c.execute('''CREATE TABLE IF NOT EXISTS rss_feeds (
        channel TEXT,
        feed_name TEXT,
        feed_url TEXT,
        fg TINYINT,
        bg TINYINT,
        enabled BOOL DEFAULT 1,
        article_title TEXT,
        article_url TEXT,
        published TEXT,
        etag TEXT,
        modified TEXT,
        PRIMARY KEY {0}
        )'''.format(primary_key))


def migrate_from_old_tables(bot, c):
    sub = bot.db.substitution

    try:
        c.execute('SELECT * FROM rss')
        oldfeeds = c.fetchall()
    except StandardError:
        oldfeeds = []

    for feed in oldfeeds:
        channel, site_name, site_url, fg, bg = feed

        # get recent article if possible
        try:
            c.execute('''
                SELECT article_title, article_url FROM recent
                WHERE channel = {0} AND site_name = {0}
                '''.format(sub), (channel, site_name))
            article_title, article_url = c.fetchone()
        except (StandardError, TypeError):
            article_title = article_url = None

        # add feed to new table
        if article_url:
            c.execute('''
                INSERT INTO rss_feeds (channel, feed_name, feed_url, fg, bg, article_title, article_url)
                VALUES ({0}, {0}, {0}, {0}, {0}, {0}, {0})
                '''.format(sub), (channel, site_name, site_url, fg, bg, article_title, article_url))
        else:
            c.execute('''
                INSERT INTO rss_feeds (channel, feed_name, feed_url, fg, bg)
                VALUES ({0}, {0}, {0}, {0}, {0})
                '''.format(sub), (channel, site_name, site_url, fg, bg))


def colour_text(text, fg, bg=''):
    """Given some text and fore/back colours, return a coloured text string."""
    if fg == '':
        return text
    else:
        colour = '{0},{1}'.format(fg, bg) if bg != '' else fg
        return "\x03{0}{1}\x03".format(colour, text)


@commands('rss')
def manage_rss(bot, trigger):
    """Manage RSS feeds. For a list of commands, type: .rss help"""
    bot.memory['rss_manager'].manage_rss(bot, trigger)


class RSSManager:
    def __init__(self, bot):
        self.running = True
        self.sub = bot.db.substitution

        # get a list of all methods in this class that start with _rss_
        self.actions = sorted(method[5:] for method in dir(self) if method[:5] == '_rss_')

    def _show_doc(self, bot, command):
        """Given an RSS command, say the docstring for the corresponding method."""
        for line in getattr(self, '_rss_' + command).__doc__.split('\n'):
            line = line.strip()
            if line:
                bot.reply(line)

    def manage_rss(self, bot, trigger):
        """Manage RSS feeds. Usage: .rss <command>"""
        if not trigger.admin:
            bot.reply("Sorry, you need to be an admin to modify the RSS feeds.")
            return

        text = trigger.group().split()
        if (len(text) < 2 or text[1] not in self.actions):
            bot.reply("Usage: .rss <command>")
            bot.reply("Available RSS commands: " + ', '.join(self.actions))
            return

        conn = bot.db.connect()
        # run the function and commit database changes if it returns true
        if getattr(self, '_rss_' + text[1])(bot, trigger, conn.cursor()):
            conn.commit()
        conn.close()

    def _rss_start(self, bot, trigger, c):
        """Start fetching feeds. Usage: .rss start"""
        bot.reply("Okay, I'll start fetching RSS feeds..." if not self.running else
                  "Continuing to fetch RSS feeds.")
        bot.debug(__file__, "RSS started.", 'verbose')
        self.running = True

    def _rss_stop(self, bot, trigger, c):
        """Stop fetching feeds. Usage: .rss stop"""
        bot.reply("Okay, I'll stop fetching RSS feeds..." if self.running else
                  "Not currently fetching RSS feeds.")
        bot.debug(__file__, "RSS stopped.", 'verbose')
        self.running = False

    def _rss_add(self, bot, trigger, c):
        """Add a feed to a channel, or modify an existing one.
        Set mIRC-style foreground and background colour indices using fg and bg.
        Usage: .rss add <#channel> <Feed_Name> <URL> [fg] [bg]
        """
        pattern = r'''
            ^\.rss\s+add
            \s+([~&#+!][^\s,]+)   # channel
            \s+("[^"]+"|[\w-]+)  # name, which can contain anything but quotes if quoted
            \s+(\S+)             # url
            (?:\s+(\d+))?        # foreground colour (optional)
            (?:\s+(\d+))?        # background colour (optional)
            '''
        match = re.match(pattern, trigger.group(), re.IGNORECASE | re.VERBOSE)
        if match is None:
            self._show_doc(bot, 'add')
            return

        channel = match.group(1)
        feed_name = match.group(2).strip('"')
        feed_url = match.group(3)
        fg = int(match.group(4)) % 16 if match.group(4) else ''
        bg = int(match.group(5)) % 16 if match.group(5) else ''

        c.execute('''
            SELECT * FROM rss_feeds WHERE channel = {0} AND feed_name = {0}
            '''.format(self.sub), (channel, feed_name))
        if not c.fetchone():
            c.execute('''
                INSERT INTO rss_feeds (channel, feed_name, feed_url, fg, bg)
                VALUES ({0}, {0}, {0}, {0}, {0})
                '''.format(self.sub), (channel, feed_name, feed_url, fg, bg))
            bot.reply("Successfully added the feed to the channel.")
        else:
            c.execute('''
                UPDATE rss_feeds SET feed_url = {0}, fg = {0}, bg = {0}
                WHERE channel = {0} AND feed_name = {0}
                '''.format(self.sub), (feed_url, fg, bg, channel, feed_name))
            bot.reply("Successfully modified the feed.")
        return True

    def _rss_del(self, bot, trigger, c):
        """Remove one or all feeds from one or all channels.
        Usage: .rss del [#channel] [Feed_Name]
        """
        pattern = r"""
            ^\.rss\s+del
            (?:\s+([~&#+!][^\s,]+))?  # channel (optional)
            (?:\s+("[^"]+"|[\w-]+))? # name (optional)
            """
        match = re.match(pattern, trigger.group(), re.IGNORECASE | re.VERBOSE)
        # at least one of channel and feed name is required
        if match is None or (not match.group(1) and not match.group(2)):
            self._show_doc(bot, 'del')
            return

        channel = match.group(1)
        feed_name = match.group(2).strip('"') if match.group(2) else None
        args = [arg for arg in (channel, feed_name) if arg]

        c.execute(('DELETE FROM rss_feeds WHERE '
                   + ('channel = {0} AND ' if channel else '')
                   + ('feed_name = {0}' if feed_name else '')
                   ).rstrip(' AND ').format(self.sub), args)

        if c.rowcount:
            noun = 'feeds' if c.rowcount != 1 else 'feed'
            bot.reply("Successfully removed {0} {1}.".format(c.rowcount, noun))
        else:
            bot.reply("No feeds matched the command.")

        return True

    def _rss_enable(self, bot, trigger, c):
        """Enable a feed or feeds. Usage: .rss enable [#channel] [Feed_Name]"""
        return self._toggle(bot, trigger, c)

    def _rss_disable(self, bot, trigger, c):
        """Disable a feed or feeds. Usage: .rss disable [#channel] [Feed_Name]"""
        return self._toggle(bot, trigger, c)

    def _toggle(self, bot, trigger, c):
        """Enable or disable a feed or feeds. Usage: .rss <enable|disable> [#channel] [Feed_Name]"""
        command = trigger.group(3)

        pattern = r"""
            ^\.rss\s+(enable|disable) # command
            (?:\s+([~&#+!][^\s,]+))?   # channel (optional)
            (?:\s+("[^"]+"|[\w-]+))?  # name (optional)
            """
        match = re.match(pattern, trigger.group(), re.IGNORECASE | re.VERBOSE)
        # at least one of channel and feed name is required
        if match is None or (not match.group(2) and not match.group(3)):
            self._show_doc(bot, command)
            return

        enabled = 1 if command == 'enable' else 0
        channel = match.group(2)
        feed_name = match.group(3).strip('"') if match.group(3) else None
        args = [arg for arg in (enabled, channel, feed_name) if arg is not None]

        c.execute(('UPDATE rss_feeds SET enabled = {0} WHERE '
                   + ('channel = {0} AND ' if channel else '')
                   + ('feed_name = {0}' if feed_name else '')
                   ).rstrip(' AND ').format(self.sub), args)

        if c.rowcount:
            noun = 'feeds' if c.rowcount != 1 else 'feed'
            bot.reply("Successfully {0}d {1} {2}.".format(command, c.rowcount, noun))
        else:
            bot.reply("No feeds matched the command.")

        return True

    def _rss_list(self, bot, trigger, c):
        """Get information on all feeds in the database. Usage: .rss list [#channel] [Feed_Name]"""
        pattern = r"""
            ^\.rss\s+list
            (?:\s+([~&#+!][^\s,]+))?  # channel (optional)
            (?:\s+("[^"]+"|[\w-]+))? # name (optional)
            """
        match = re.match(pattern, trigger.group(), re.IGNORECASE | re.VERBOSE)
        if match is None:
            self._show_doc(bot, 'list')
            return

        channel = match.group(1)
        feed_name = match.group(2).strip('"') if match.group(2) else None

        c.execute('SELECT * FROM rss_feeds')
        feeds = [RSSFeed(row) for row in c.fetchall()]

        if not feeds:
            bot.reply("No RSS feeds in the database.")
            return

        filtered = [feed for feed in feeds
                    if (feed.channel == channel or channel is None)
                    and (feed_name is None or feed.name.lower() == feed_name.lower())]

        if not filtered:
            bot.reply("No feeds matched the command.")
            return

        noun = 'feeds' if len(feeds) != 1 else 'feed'
        bot.reply("Showing {0} of {1} RSS {2} in the database:".format(
            len(filtered), len(feeds), noun))

        for feed in filtered:
            bot.say("  {0} {1} {2}{3} {4} {5}".format(
                    feed.channel,
                    colour_text(feed.name, feed.fg, feed.bg),
                    feed.url,
                    " (disabled)" if not feed.enabled else '',
                    feed.fg, feed.bg))

    def _rss_fetch(self, bot, trigger, c):
        """Force all RSS feeds to be fetched immediately. Usage: .rss fetch"""
        read_feeds(bot, True)

    def _rss_help(self, bot, trigger, c):
        """Get help on any of the RSS feed commands. Usage: .rss help <command>"""
        command = trigger.group(4)
        if command in self.actions:
            self._show_doc(bot, command)
        else:
            bot.reply("For help on a command, type: .rss help <command>")
            bot.reply("Available RSS commands: " + ', '.join(self.actions))


class RSSFeed:
    """Represent a single row in the feed table."""

    def __init__(self, row):
        """Initialize with values from the feed table."""
        columns = ('channel',
                   'name',
                   'url',
                   'fg',
                   'bg',
                   'enabled',
                   'title',
                   'link',
                   'published',
                   'etag',
                   'modified',
                   )
        for i, column in enumerate(columns):
            setattr(self, column, row[i])


@interval(INTERVAL)
def read_feeds(bot, force=False):
    if not bot.memory['rss_manager'].running and not force:
        return

    sub = bot.db.substitution
    conn = bot.db.connect()
    c = conn.cursor()
    c.execute('SELECT * FROM rss_feeds')
    feeds = c.fetchall()
    if not feeds:
        bot.debug(__file__, "No RSS feeds to check.", 'warning')
        return

    for feed_row in feeds:
        feed = RSSFeed(feed_row)
        if not feed.enabled:
            continue

        def disable_feed():
            c.execute('''
                UPDATE rss_feeds SET enabled = {0}
                WHERE channel = {0} AND feed_name = {0}
                '''.format(sub), (0, feed.channel, feed.name))
            conn.commit()

        try:
            fp = feedparser.parse(feed.url, etag=feed.etag, modified=feed.modified)
        except IOError as e:
            bot.debug(__file__, "Can't parse feed on {0}, disabling ({1})".format(
                feed.name, str(e)), 'warning')
            disable_feed()
            continue

        # fp.status will only exist if pulling from an online feed
        status = getattr(fp, 'status', None)

        bot.debug(feed.channel, "{0}: status = {1}, version = '{2}', items = {3}".format(
            feed.name, status, fp.version, len(fp.entries)), 'verbose')

        # check HTTP status
        if status == 301:  # MOVED_PERMANENTLY
            bot.debug(
                __file__,
                "Got HTTP 301 (Moved Permanently) on {0}, updating URI to {1}".format(
                feed.name, fp.href), 'warning')
            c.execute('''
                UPDATE rss_feeds SET feed_url = {0}
                WHERE channel = {0} AND feed_name = {0}
                '''.format(sub), (fp.href, feed.channel, feed.name))
            conn.commit()

        elif status == 410:  # GONE
            bot.debug(__file__, "Got HTTP 410 (Gone) on {0}, disabling".format(
                feed.name), 'warning')
            disable_feed()

        if not fp.entries:
            continue

        feed_etag = getattr(fp, 'etag', None)
        feed_modified = getattr(fp, 'modified', None)

        entry = fp.entries[0]
        # parse published and updated times into datetime objects (or None)
        entry_dt = (datetime.fromtimestamp(time.mktime(entry.published_parsed))
                    if hasattr(entry, 'published_parsed') else None)
        entry_update_dt = (datetime.fromtimestamp(time.mktime(entry.updated_parsed))
                           if hasattr(entry, 'updated_parsed') else None)

        # check if article is new, and skip otherwise
        if (feed.title == entry.title and feed.link == entry.link
                and feed.etag == feed_etag and feed.modified == feed_modified):
            bot.debug(__file__, u"Skipping previously read entry: [{0}] {1}".format(
                feed.name, entry.title), 'verbose')
            continue

        # save article title, url, and modified date
        c.execute('''
            UPDATE rss_feeds
            SET article_title = {0}, article_url = {0}, published = {0}, etag = {0}, modified = {0}
            WHERE channel = {0} AND feed_name = {0}
            '''.format(sub), (entry.title, entry.link, entry_dt, feed_etag, feed_modified,
                              feed.channel, feed.name))
        conn.commit()

        if feed.published and entry_dt:
            published_dt = datetime.strptime(feed.published, "%Y-%m-%d %H:%M:%S")
            if published_dt >= entry_dt:
                # This will make more sense once iterating over the feed is
                # implemented. Once that happens, deleting or modifying the
                # latest item would result in the whole feed getting re-msg'd.
                # This will prevent that from happening.
                bot.debug(__file__, u"Skipping older entry: [{0}] {1}, because {2} >= {3}".format(
                    feed.name, entry.title, published_dt, entry_dt), 'verbose')
                continue

        # create message for new entry
        message = u"[\x02{0}\x02] \x02{1}\x02 {2}".format(
            colour_text(feed.name, feed.fg, feed.bg), entry.title, entry.link)

        # append update time if it exists, or published time if it doesn't
        timestamp = entry_update_dt or entry_dt
        if timestamp:
            # attempt to get time format from preferences
            tformat = ''
            if feed.channel in bot.db.preferences:
                tformat = bot.db.preferences.get(feed.channel, 'time_format') or tformat
            if not tformat and bot.config.has_option('clock', 'time_format'):
                tformat = bot.config.clock.time_format

            message += " - {0}".format(timestamp.strftime(tformat or '%F - %T%Z'))

        # print message
        bot.msg(feed.channel, message)

    conn.close()
