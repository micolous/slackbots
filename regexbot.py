#!/usr/bin/env python
"""
regexbot: Slack-based regular expression evaluation tool.
Copyright 2010 - 2015 Michael Farrell <http://micolous.id.au>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

This is a Slack port of the original, IRC based version (which predates Slack).
"""


import regex, asyncore, threading, inspect, ctypes, time
from argparse import ArgumentParser
from copy import copy
from datetime import datetime, timedelta
from configparser import RawConfigParser
from slackrealtime import connect, RtmProtocol
from slackrealtime.event import Message
from slackrealtime.factory import DyingWebSocketClientFactory
from string import maketrans, translate
from subprocess import Popen, PIPE
from sys import argv, exit, stdout
import traceback
from twisted.internet import reactor
from twisted.python import log

DEFAULT_CONFIG = {
	'regexbot': {
		'channel_flood_cooldown': 5,
		'global_flood_cooldown': 1,
		'max_messages': 25,
		'max_message_size': 200,
		'version': 'regexbot; https://github.com/micolous/slackbots/',
	}
}


# main code
class RegexBot(object):
	def __init__(self, config_path=None):
		if config_path is None:
			config_path = 'regexbot.ini'
		config = RawConfigParser()
		config.read_dict(DEFAULT_CONFIG)
		config.read(config_path)

		self.rtm_token = config.get('regexbot', 'rtm_token')

		self.channel_flood_cooldown = timedelta(seconds=config.getint('regexbot', 'channel_flood_cooldown'))
		self.global_flood_cooldown = timedelta(seconds=config.getint('regexbot', 'global_flood_cooldown'))
		self.max_messages = config.getint('regexbot', 'max_messages')
		self.max_message_size = config.getint('regexbot', 'max_message_size')

		self.version = str(config.get('regexbot', 'version')) + '; %s'
		try: self.version = self.version % Popen(["git","branch","-v","--contains"], stdout=PIPE).communicate()[0].strip()
		except: self.version = self.version % 'unknown'

		self._last_message_times = {}
		self._last_message = datetime.utcnow()
		self._message_buffer = {}

		self.ignore_list = []
		if config.has_section('ignore'):
			for k,v in config.items('ignore'):
				try:
					self.ignore_list.append(regex.compile(str(v), regex.I))
				except Exception, ex:
					print "Error compiling regular expression in ignore list (%s):" % k
					print "  %s" % v
					print ex
					exit(1)

	def flood_control(self, channel, when):
		"Implements flood controls.  Returns True if the message should be handled, returns False if the floods are in."
		# get delta
		channel_delta = when - self._last_message_times.get(channel, self._last_message)
		global_delta = when - self._last_message

		# update times
		self._last_message = self._last_message_times[channel] = when

		# think global
		if global_delta < self.global_flood_cooldown:
			print "Global flood protection hit, %s of %s seconds were waited" % (global_delta.seconds, self.global_flood_cooldown.seconds)
			return False

		# act local
		if channel_delta < self.channel_flood_cooldown:
			print "Local %s flood protection hit, %s of %s seconds were waited" % (channel, channel_delta.seconds, self.channel_flood_cooldown.seconds)
			return False

		# we're cool.
		return True

	def message_handler(self, msg, user_id, channel_id):
		now = datetime.utcnow()
		if channel_id not in self._message_buffer:
			# New channel, configure buffer
			self._message_buffer[channel_id] = []

		if msg.startswith(self.protocol.meta.me['name']):
			lmsg = msg.lower()

			if 'help' in lmsg or 'info' in lmsg or '?' in lmsg:
				if not self.flood_control(channel_id, now):
					return

				# give information
				return '<@%s>: I am regexbot, the interactive Slack regular expression tool, originally written by micolous.  Source/docs/version: %s' % (user_id, self.version)
			return

		str_replace = False
		str_translate = False

		if msg.startswith('s'):
			str_replace = True
		if msg.startswith('y'):
			str_translate = True
	
		valid_separators = ['@','#','%',':',';','/','\xe1']
		separator = '/'
		if (str_replace or str_translate) and len(msg) > 1 and msg[1] in valid_separators:
			separator = msg[1]
		else:
			str_replace = False
			str_translate = False

		if (str_replace or str_translate) and msg[1] == separator:
			for item in self.ignore_list:
				if item.search(user_id) != None:
					# ignore list item hit
					print "Ignoring message from %s because of: %s" % (user_id, item.pattern)
					return

			# handle regex
			parts = msg.split(separator)

			if not self.flood_control(channel_id, now):
				return

			if len(self._message_buffer[channel_id]) == 0:
				return '<@%s>: message buffer is empty' % user_id

			if len(parts) == 3:
				return '<@%s>: invalid expression, you forgot the trailing separator, dummy' % user_id

			if len(parts) != 4:
				# not a valid regex
				return '<@%s>: invalid expression, not the right amount of separators' % user_id


			# find messages matching the string
			if len(parts[1]) == 0:
				return '<@%s>: original string is empty' % user_id

			if str_replace:
				ignore_case = 'i' in parts[3]
				e = None
				try:
					if ignore_case:
						e = regex.compile(parts[1], regex.I)
					else:
						e = regex.compile(parts[1])
				except Exception, ex:
					return '<@%s>: failure compiling regular expression: %s' % (user_id, ex)

				# now we have a valid regular expression matcher!
				timeout = time.time() + 10
				for x in range(len(self._message_buffer[channel_id])-1, -1, -1):
					if time.time() > timeout: break
					result = [None,None]
					thread = RegexThread(e,parts[2],self._message_buffer[channel_id][x][1],result)
					thread.start()
					try:
						thread.join(0.1)
						while thread.isAlive():
							thread.raiseExc(TimeoutException)
							time.sleep(0.1)

						if result[0] == None or result[1] == None:
							continue

					except Exception, ex:
						return '<@%s>: failure replacing: %s' % (user_id, ex)

					new_message = []
					# replace the message in the buffer
					new_message = [self._message_buffer[channel_id][x][0], result[1].replace('\n','').replace('\r','')[:self.max_message_size], self._message_buffer[channel_id][x][2]]
					for c in new_message[1]:
						if ord(c) < 0x20:
							return '<@%s>: hiiii rails' % (user_id,)

					del self._message_buffer[channel_id][x]
					self._message_buffer[channel_id].append(new_message)

					# now print the new text
					print new_message
					if new_message[2]:
						# action
						# FIXME: probably won't work on slack
						return ((' * <@%s> %s' % (new_message[0], new_message[1]))[:self.max_message_size])
					else:
						# normal message
						return new_message[1][:self.max_message_size], new_message[0]
					return

			if str_translate:
				if len(parts[1]) != len(parts[2]) or len(parts[1]) < 1:
					return '<@%s>: Translation is different length!' % user_id

				# make translation table
				table = maketrans(parts[1], parts[2])

				for num in xrange(len(self._message_buffer[channel_id])-1, -1, -1):
					# make new message, test if changes occur; if not, continue
					result = translate(self._message_buffer[channel_id][num][1], table)
					if result == self._message_buffer[channel_id][num][1]:
						continue
				
					# build new message, and insert into buffer
					new_message = [self._message_buffer[channel_id][num][0], result.replace('\n','').replace('\r','')[:self.max_message_size], self._message_buffer[channel_id][num][2]]
					for c in new_message[1]:
						if ord(c) < 0x20:
							return '<@%s>: hiiii rails' % (user_id,)

					del self._message_buffer[channel_id][num]
					self._message_buffer[channel_id].append(new_message)

					# print new message and send to server
					print new_message
					if new_message[2]:
						# action
						return ((' * <@%s> %s' % (new_message[0], new_message[1]))[:self.max_message_size])
					else:
						# normal message
						return (('<@%s> %s' % (new_message[0], new_message[1]))[:self.max_message_size])
					return

			# no match found
			return '<@%s>: no match found' % user_id


		else:
			# add to buffer
			self._message_buffer[channel_id].append([user_id, msg[:self.max_message_size], False])
		
		# trim the buffer
		self._message_buffer[channel_id] = self._message_buffer[channel_id][-self.max_messages:]



# from http://stackoverflow.com/questions/323972/is-there-any-way-to-kill-a-thread-in-python/325528#325528
def _async_raise(tid, exctype):
	'''Raises an exception in the threads with id tid'''
	if not inspect.isclass(exctype):
		raise TypeError("Only types can be raised (not instances)")
	res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.py_object(exctype))
	if res == 0:
		raise ValueError("invalid thread id")
	elif res != 1:
		# "if it returns a number greater than one, you're in trouble,
		# and you should call it again with exc=NULL to revert the effect"
		ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, 0)
		raise SystemError("PyThreadState_SetAsyncExc failed")

class RegexThread(threading.Thread):
	def __init__(self,regex,replace,message,result):
		threading.Thread.__init__(self)
		self.regex = regex
		self.replace = replace
		self.message = message
		self.result = result

	def run(self):
		try:
			self.result[0] = self.regex.search(self.message)
		except MemoryError:
			self.result[0] = None
			return
		if self.result[0] != None:
			self.result[1] = self.regex.sub(self.replace,self.message)

	def raiseExc(self, exctype):
		if not self.isAlive():
			raise threading.ThreadError("the thread is not active")
		_async_raise( self.ident, exctype )

class TimeoutException(Exception):
	pass

class RegexBotProtocol(RtmProtocol):
	def init(self, bot):
		self.bot = bot
		self.bot.protocol = self
		return self

	def onOpen(self):
		# We don't actually want a welcome message
		pass

	def onSlackEvent(self, event):
		if not isinstance(event, Message):
			# Only care about messages
			return

		if not hasattr(event, 'user') or event.user == 'USLACKBOT' or event.user == self.meta.me['id']:
			# Ignore system messages
			return

		try:
			response = self.bot.message_handler(event.text, event.user, event.channel)
		except Exception, ex:
			print 'Exception handled:'
			traceback.print_exc()
			return

		if response is not None:
			if isinstance(response, str) or isinstance(response, unicode):
				self.sendChatMessage(response, id=event.channel)
			else:
				# Returns tuple of message, user_id
				# Resolve the UserID and post as them. ;)
				user = self.meta.users.get(response[1], None)
				if user is None:
					self.sendChatMessage(response, id=event.channel)
				else:
					self.sendChatMessage(response[0], id=event.channel, username=u'%s (regexbot)' % user['name'], icon_url=user['profile']['image_48'])

	def sendChatMessage(self, *args, **kwargs):
		# Always use the REST API to send a message
		kwargs['send_with_api'] = True
		kwargs['unfurl_links'] = False
		kwargs['unfurl_media'] = False
		if 'username' not in kwargs:
			kwargs['username'] = 'regexbot'
		kwargs['link_names'] = False
		if 'icon_url' not in kwargs and 'icon_emoji' not in kwargs:
			kwargs['icon_emoji'] = ':camel:'
		return RtmProtocol.sendChatMessage(self, *args, **kwargs)


def main():
	parser = ArgumentParser()
	parser.add_argument(
		'-c', '--config',
		default='regexbot.ini',
		help='Path to configuration file to use for regexbot. [default: %(default)s]',
	)
	options = parser.parse_args()
	log.startLogging(stdout)
	regexbot = RegexBot(options.config)

	conn = connect(
		regexbot.rtm_token,
		protocol=lambda *a,**k: RegexBotProtocol(*a,**k).init(regexbot),
		factory=DyingWebSocketClientFactory
	)
	reactor.run()

if __name__ == '__main__':
	main()

