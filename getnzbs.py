#!/usr/bin/env python3
#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# getnzbs.py
#
# Henry Eissler III
# version: 0.4.9
# 1/18/2025
#
# query Newznab servers
# (see getnzbs.py --help for query options)
#
# uses ncurses interface.  120 columns or more preferred
# spacebar queues item -- return fetches
#
# non-standard requirements:
#   - curseslistwindow  <https://github.com/heissler3/curseslistwindow>
#   - configobj         <https://pypi.org/project/configobj/>

import os, sys, argparse
import threading, queue
import curses
from curseslistwindow import *
from configobj import ConfigObj as CfgObj
from time import sleep
import urllib.request as urlreq
from urllib.error import *
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

#~~~ Constants ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
version = '0.4.9'
ua = 'getnzbs/' + version   # UserAgent HTTP header value
config_file_paths = [ './getnzbs.conf', os.environ['HOME']+'/.config/getnzbs.conf', ]

#~~~ Globals ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
results = []        # query results - a list of dicts
                    #   keys: 'title', 'pubDate', 'link',
                    #         'category', 'size', 'fetched'
displaylist = []    # strings (or chars) to display
totalscreen = None  # primary curses window
mainwin = None
headerwin = None
listwin = None
footerwin = None
columns = [4, 1, 0, 22, 11]
displayqueue = queue.SimpleQueue()

#~~~ Curses functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def init_screen():
    scr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    scr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        # color pairs 13 - 15 are used and defined by curseslistwindow
        # the following are specific to this app
        curses.init_pair(1, curses.COLOR_WHITE,     # header & status messages
                            curses.COLOR_BLUE)
        curses.init_pair(2, curses.COLOR_WHITE,     # "please wait..."
                            curses.COLOR_RED)
        curses.init_pair(4, curses.COLOR_YELLOW,    # fetched & alert messages
                            curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_RED,       # alert border
                            curses.COLOR_BLACK)
        curses.init_pair(12, curses.COLOR_BLACK,     # fetched && current
                             curses.COLOR_YELLOW)
    curses.curs_set(0)
    #curses.mousemask(0x00210002) # BUTTON1_PRESSED | scrollup | scrolldown
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    scr.nodelay(True)
    return scr

def divide_screen(scr):
    global headerwin, mainwin, footerwin
    scr.clear()
    headerrows = 2
    footerrows = 1
    maxrows, maxcols = scr.getmaxyx()
    mainrows = maxrows - (headerrows + footerrows)
    
    if headerwin:
        headerwin.resize(headerrows, maxcols)
        win1 = headerwin
    else:
        win1 = curses.newwin(headerrows, maxcols, 0, 0)
        win1.attron(curses.color_pair(1)|curses.A_BOLD)
        win1.bkgd(' ',curses.color_pair(1)|curses.A_BOLD)
    # scr.hline(2, 0, curses.ACS_HLINE, maxcols)

    if mainwin:
        mainwin.resize(mainrows, maxcols)
        win2 = mainwin
    else:
        win2 = curses.newwin(mainrows, maxcols, headerrows, 0)
    # scr.hline(maxrows-2, 0, curses.ACS_HLINE, maxcols)

    if footerwin:
        footerwin.resize(footerrows, maxcols)
        footerwin.mvwin(maxrows-1, 0)
        win3 = footerwin
    else:
        win3 = curses.newwin(footerrows, maxcols, maxrows-1, 0)
    
    scr.refresh()
    return win1, win2, win3

def demolish_screen(scr):
    curses.nocbreak()
    scr.keypad(False)
    curses.echo()
    curses.endwin()

def write_header(message, attr):
    global headerwin
    maxcol = headerwin.getmaxyx()[1] - 1
    headerwin.move(0, 0)
    headerwin.clrtoeol()
    if len(message) < maxcol:
        headerwin.addstr(0, 1, message, (attr | curses.A_BOLD))
    headerwin.refresh()    

def write_status(status):
    """
    for debugging purposes only
    """
    global headerwin
    maxcol = headerwin.getmaxyx()[1] - 1
    headerwin.move(1, 0)
    headerwin.clrtoeol()
    if len(status) < maxcol:
        headerwin.addstr(1, 1, status, curses.A_BOLD)
        headerwin.refresh()

def write_footer(message):
    global footerwin
    footerline = footerwin.getmaxyx()[0] - 1
    footerwin.addstr(footerline, 5, message)
    footerwin.clrtoeol()
    footerwin.refresh()

def display_alert(messages):
    """
    Display an alert (red border, yellow text)
                     (colorpair 5, colorpair 4)
        in the center of the screen
        and return a single key char response.
    1 parameter, messages, is a list of strings
        to display
    """
    global totalscreen
    (my, mx) = totalscreen.getmaxyx()
    cy = int(my/2)
    cx = int(mx/2)
    aheight = len(messages) + 4
    awidth = max(map(len, messages)) + 4
    atop = cy - int(aheight/2)
    aleft = cx - int(awidth/2)
    alert = curses.newwin(aheight, awidth, atop, aleft)
    alert.attron(curses.color_pair(5))
    alert.box()
    alert.attroff(curses.color_pair(5))
    alert.bkgd(' ', curses.color_pair(-1))
    alert.attron(curses.color_pair(4))
    for (i, msg) in enumerate(messages):
        x = int((awidth/2) - (len(msg)/2))
        alert.addstr( 2+i, x, msg )
    alert.attroff(curses.color_pair(4))
    alert.refresh()
    response = totalscreen.getch()
    alert.erase()
    return chr(response)

#~~~ Curses Objects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
class NzbHeaderListWindow(MultiColumnListWindow):
    """
    multi-column curses window; specialized, not adaptable,
    so 'colwidths' is hard-coded, not passed in.
    data is a list of lists of strings or chars
    'len(data[n])' == 'len(colwidths)' == 'numcols',
    so there is some redundancy.
    """

    def __init__(self, window, data):
        super().__init__(window, data, colwidths=columns)
        self.fetched = [False for x in range(len(data))]

    def write_row(self, index):
        details = self.list[index]
        line = index - self.offset
        if line < 0 or line > (self.line_count - 1):
            return
        attr = (curses.color_pair(13)|curses.A_BOLD) if (index == self.current) else 0
        if self.fetched[index]:
            details[1] = 'X'
            if index != self.current:
                attr = curses.color_pair(4)
            else:
                attr = curses.color_pair(12)|curses.A_BOLD
        elif self.selected[index]:
            details[1] = '-'
            if index == self.current:
                attr = curses.color_pair(14)|curses.A_BOLD
            else:
                attr = curses.color_pair(15)|curses.A_BOLD
        else:
            details[1] = ' '
        for n in range(self.numcols):
            self.subwin[n].move(line, 0)
            self.subwin[n].clrtoeol()
            if len(details[n]) > self.colwidths[n]:
                self.subwin[n].insnstr(details[n], self.colwidths[n], attr)
            elif len(details[n]) > 1:
                self.subwin[n].insstr(details[n], attr)
            else:
                self.subwin[n].insch(details[n], attr)

    def new_data(self, data):
        self.list = data
        self.list_length = len(data)
        self.selected = [False for x in range(self.list_length)]
        self.fetched = [False for x in range(len(data))]
        maxrows = self.win.getmaxyx()[0]
        if self.drawborder:
            maxrows -= 2
        self.line_count = min(self.list_length, maxrows)

    def write_status_spinner(self, index, thread):
        i = 0
        while thread.is_alive():
            line = index - self.offset
            if line >= 0 and line < self.line_count:
                ch = ('|', '/', '-', '\\')[i % 4]
                displayqueue.put((self.subwin[1].delch, (line, 0)))
                displayqueue.put((self.subwin[1].insch, (line, 0, ch)))
                displayqueue.put((self.subwin[1].noutrefresh, ()))
                i += 1
            sleep(.25)

#~~~ Misc. functions  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def config_not_found():
    ( txtnorm, txtbold, txtred, txtblue, txtamber, ) = map(lambda s: "\x1b["+s, [ "0m", "1;37m", "1;31m", "1;34m", "33m", ])
    print("Configuration file not found.\n", file=sys.stderr)
    yn = input(f"Would you like to create one? {txtbold}(y/n){txtnorm}: ")
    if not yn or yn.lower()[0] != 'y':
        print("Well, sorry, but no servers are defined, so there's nothing to do.\n", file=sys.stderr)
        return None
    config = CfgObj()
    config.filename = config_file_paths[0]
    config['defaults'] = {}
    config['servers'] = {}
    resp = input(f"Please enter the directory path to save nzbs in ({txtbold}default{txtamber} {os.environ['HOME']}/Downloads/nzbs/){txtnorm}: ")
    if not resp:
        destdir = os.environ['HOME'] + '/Downloads/nzbs/'
    elif not os.path.isdir(resp):
        yn = input(f"Sorry, {resp} directory does not exist.  Create it? {txtbold}(y/n){txtnorm}: ")
        if yn and yn.lower()[0] == 'y':
            try:
                os.mkdir(resp)
                destdir = resp
            except Exception as exc:
                print(f"{txtred}Error!{txtnorm} {type(exc)}: trying to create directory {resp}!\n{exc}\n\nBailing now.\n", file=sys.stderr)
                return None
    else:
        destdir = resp
    config['defaults']['DestinationDirectory'] = destdir
    resp = input(f"Please enter the maximum number of results per request ({txtbold}default{txtnorm}: 300): ")
    if not resp:
        maxresults = 300
    elif not resp.isdecimal():
        print(f"{resp} is not a number.  Moving on...")
        maxresults = 100
    config['defaults']['MaxResults'] = maxresults
    enterserver = ( input(f"Would you like to enter server information? {txtbold}(y/n){txtnorm}? ").lower().startswith('y') )
    while enterserver:
        serv_name = input(f"Please enter a name for the server: ")
        serv_url = input(f"Please enter a valid api url for the server: ")
        serv_key = input(f"Finally, please enter your api key for the server (or hit return): ")
        serv_key = serv_key or ''
        config['servers'][serv_name] = {}
        config['servers'][serv_name]['URL'] = serv_url
        config['servers'][serv_name]['ApiKey'] = serv_key
        enterserver = ( input(f"Would you like to enter another? {txtbold}(y/n){txtnorm}? ").lower().startswith('y') )
    config.write()
    return config

def dispatch_fetch():
    """
    to be run in a seperate thread
    which creates another thread,
    and then calls the spinner routine
    """
    global  results, listwin, headerwin, totalscreen
    mx = headerwin.getmaxyx()[1] - 1
    for idx in range(len(results)):
        if listwin.selected[idx]:
            fetch = FetchNZBThread(results[idx])
            fetch.start()
            listwin.write_status_spinner(idx, fetch)

            if fetch.success:
                listwin.fetched[idx] = True
            listwin.selected[idx] = False
            displayqueue.put((listwin.write_row, (idx,)))
            displayqueue.put((listwin.refresh_list, ()))

def monitor_display_queue():
    """
    to be run in a seperate thread
    from the key input loop,
    so that they can both be blocking
    """
    while True:
        (wfunc, cargs) = displayqueue.get(True) # blocking
        if len(cargs) > 0:
            wfunc(*cargs)
        else:
            if wfunc == -1:
                break
            else:
                wfunc()
        curses.doupdate()

def choose_category():
    """
    called if the user chose -b or --browse option
    instead of a search query
    """
    global listwin, mainwin, parameters, displaylist
    categories = []

    listwin = SelectFromListWindow(mainwin, displaylist)
    listwin.draw_window()
    write_status("Retrieving Category List")

    capsquery = servers[args.server]['URL'] + '/api?' + urlencode({'t':'caps','apikey':servers[args.server]['ApiKey']})
    capsrequest = urlreq.Request(capsquery, headers={'User-Agent':ua})

    try:
        capsresponse = urlreq.urlopen(capsrequest).read().decode()
        xmlroot = ET.fromstring(capsresponse)
        qresults = xmlroot.findall('.//category')
    except (URLError, HTTPError) as e:
        print("Retrieval Error " + e)
        exit(1)

    for cat in qresults:
        for subcat in cat.iter():
            d = subcat.attrib
            d['type'] = subcat.tag
            categories.append(d)
            displayline = "{:<4}:  {:<24}".format(d['id'], d['name'])
            if d['type'] == 'subcat':
                displayline = ' '*4 + displayline
            displaylist.append(displayline)

    listwin.new_data(displaylist)
    listwin.draw_list()
    write_footer("Press 'Q' to quit,  'Space' to select,  'Enter' to choose")
    while True:
        key = totalscreen.getch()
        handled = listwin.keypress(key)
        if not handled:
            if key in (ord('q'), ord('Q')):
                demolish_screen(totalscreen)
                exit(0)
            elif key in (ord('\n'), curses.KEY_ENTER):
                cats = []
                for sc in range(len(listwin.selected)):
                    if listwin.selected[sc]:
                        cats.append(categories[sc]['id'])
                parameters['cat'] = ','.join(cats)
                displaylist = []
                write_status('')
                return
            elif key == curses.KEY_RESIZE:
                (headerwin, mainwin, fooerwin) = divide_screen(totalscreen)
                listwin = SelectFromListWindow(mainwin, displaylist)
                listwin.draw_window()

def human(size):
    """ Return human-readable string for byte size value
    with appropriate suffix.

    Takes a float as input
    """
    for suffix in ['', 'K', 'M', 'G', 'T']:
        if abs(size) < 1024.0:
            return "{:3.2f} {:}B".format(size, suffix)
        size /= 1024
    return "!!!!!!"  # we ain't doin' Petabyte downloads!

def choose_to_exit():
    """
    Once the user presses 'Q'
    check if there's anything in the download queue
    and if so warn them first
    """
    global listwin
    count = 0
    for idx in range(len(results)):
        if listwin.selected[idx]:
            count += 1
    if count == 0:
        quit_this(0)
    else:
        msgs = [ f"{count} items are queued.", "Are you sure? [Y/N]" ]
        response = display_alert(msgs)
        if ( response in ['Y', 'y'] ):    
            quit_this(count)
        # else:
        listwin.draw_window()

def quit_this(status):
    """
    Actually close the app
    """
    displayqueue.put((-1,()))
    demolish_screen(totalscreen)
    exit(status)

#~~~ Background threads  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
class FetchQueryThread(threading.Thread):

    def __init__(self, baseurl, params, offset, limit, pgsize=100):
        self.serverurl = baseurl
        self.params = params
        self.offset = offset
        self.limit = limit
        self.pgsize = pgsize    # Apparently not the same for every server
        self.success = False
        self.error = "none"
        threading.Thread.__init__(self)

    def run(self):
        #write_status(f"{self.params['q']} from {self.serverurl}")
        allresults = []
        remaining = self.limit
        pgsize = self.pgsize
        qresults = []
        # begin = int(self.params['offset'])
        # page = 0
        while remaining > 0:
            if remaining < pgsize:
                self.params['limit'] = remaining        # on the last page
            self.params['offset'] = str(self.offset)
            qurl = self.serverurl + '/api?' + urlencode(self.params)
            qreq = urlreq.Request(qurl, headers={'User-Agent':ua})

            # Fetch
            try:
                qresp = urlreq.urlopen(qreq)
                qoutput = qresp.read().decode()
            except (URLError, HTTPError) as e:
                self.success = False
                self.error = "Fetch Error: " + str(e)
                exit()
            except Exception as exc:
                self.success = False
                self.error = "WTF: "+str(exc)
                exit()

            # Parse
            try:
                xroot = ET.fromstring(qoutput)
                qresults = xroot.findall('./channel/item')
            except ET.ParseError:
                self.success = False
                self.error = "ParseError\n"\
                           + "Server returned non-XML:\n"\
                           + qoutput
                exit()
            except ValueError as ve:
                self.success = False
                self.error = "XML error: " + str(ve) + "\n"\
                           + "Page: " + str(page) + "\n"\
                           + "Total Results: " + str(len(allresults))
                exit()

            # Store
            for result in qresults:
                item = {}
                for k in ['title', 'pubDate', 'link', 'category']:
                    item[k] = result.find(k).text
                item['size'] = int(result.find('enclosure').get('length'))
                item['fetched'] = False
                allresults.append(item)

            if len(qresults) < pgsize:
                # either that's all there is,
                # or the limit has been reached
                break

            self.offset += pgsize
            remaining -= pgsize
        self.results = allresults
        self.success = True

class FetchNZBThread(threading.Thread):
    global destdir

    def __init__(self, item):
        self.url = item['link'].replace('&amp;', '&')
        self.title = item['title']
        self.destpath = destdir + item['title'] + '.nzb'
        self.success = False
        threading.Thread.__init__(self)

    def run(self):
        displayqueue.put((write_status, (self.title,)))
        nzbreq = urlreq.Request(self.url, headers={'User-Agent':ua})
        try:
            nzb = urlreq.urlopen(nzbreq)
            with open(self.destpath, 'wb') as nzbfile:
                nzbfile.write(nzb.read())
            self.success = True
            displayqueue.put((write_status, ('',)))
        except (URLError, HTTPError) as e:
            displayqueue.put((write_status, (str(e),)))
            self.success = False

#~~~ Load Configuration ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
for cf in config_file_paths:
    if os.path.isfile(cf):
        config = CfgObj(cf)
        break
else:
    config = config_not_found()
    if not config:
        exit()
destdir = config['defaults']['DestinationDirectory']
if not destdir.endswith('/'):
    destdir = destdir + '/'
if not os.path.isdir(destdir):
    try:
        os.mkdir(destdir)
    except:
        print(f"Directory {destdir} does not exist and cannot be created.\n"
                f"Defaulting to current directory: {os.getcwd()}", file=sys.stderr)
        destdir = os.getcwd()
default_max = int(config['defaults']['MaxResults'])
servers = config['servers']

#~~~ Command Line Options  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
cli_arguments = [ ("query", {'nargs':'*', 'help':"search term[s]"}),
                  ("-s", "--server", {'choices':list(servers), 'default':list(servers)[0], 'help':"nzb server to query"}),
                  ("--cat", {'dest':"category", 'help':"category to search or browse"}),
                  ("-b", "--browse", {'action':"store_true", 'help':"browse categories"}),
                  ("-l", "--limit", {'type':int, 'default':default_max, 'help':"limit number of results returned"}),
                  ("-o", "--offset", {'type':int, 'default':0, 'help':"skip the first N results"}),
                  ("-m", {'action':"store_true", 'dest':"movie", 'help':"search movies"}),
                  ("--imdb", {'dest':"imdbid", 'help':"imdb.com id (implies -m),"}),
                  ("--tvdb", {'dest':"tvdbid", 'help':"tvdb.com id  (implies -t),"}),
                  ("-t", {'action':"store_true", 'dest':"tv", 'help':"search tv shows"}),
                  ("-S", "--season", {'help':"season number  (requires -t),"}),
                  ("-E", "--episode", {'help':"episode number  (requires -t & -s),"}),
                  ("-a", "--anime", {'action':"store_true", 'help':"shorthand for category '5070': Anime"}),
                  ("--book", {'action':"store_true", 'help':"search books"}),
                  ("--author", {'nargs':'+', 'help':"author (case insensitive),"}),
                  ("-c", "--comics", {'action':"store_true", 'help':"shorthand for category '7030': Comics"}),
                  ("--music", {'action':"store_true", 'help':"search music"}),
                  ("--artist", {'nargs':'+', 'help':"artist (case insensitive),"}),
                  ("-r", {'action':"store_true", 'dest':"reverse", 'help':"reverse sort"}),
                  ("--alpha", {'action':"store_true", 'help':"sort alphabetically"}),
                  ("--version", "-V", {'action':"store_true", 'help':"report version and exit"}),
                ]
clparser = argparse.ArgumentParser(description="query Newznab and compatible servers")
for clarg in cli_arguments:
    clparser.add_argument( *clarg[0:-1], **clarg[-1] )
args = clparser.parse_args()

if args.version:
    print("getnzbs version " + version)
    exit(0)

#~~~ Setup Query per Options ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
server = servers[args.server]
serverurl, apikey = [ server[v] for v in ('URL', 'ApiKey') ]
pgsize = int(server['PageSize']) if 'PageSize' in server else 100

parameters = {'t': 'search'}
parameters['apikey'] = apikey
if args.query:
    parameters['q'] = ' '.join(args.query)

if args.tv or args.tvdbid:
    parameters['t'] = 'tvsearch'
    if args.tvdbid:
        parameters['tvdbid'] = args.tvdbid
    if args.season:
        parameters['season'] = args.season
        if args.episode:
            parameters['ep'] = args.episode
elif args.movie or args.imdbid:
    parameters['t'] = 'movie'
    if args.imdbid:
        if args.imdbid.startswith('tt'):
            args.imdbid = args.imdbid.lstrip('tt')
        parameters['imdbid'] = args.imdbid
elif args.book or args.author:
    parameters['t'] = 'book'
    parameters['cat'] = '7020'
    if args.author:
        parameters['author'] = ','.join(args.author)
elif args.music or args.artist:
    parameters['t'] = 'music'
    parameters['cat'] = '3010,3040'
    if args.artist:
        parameters['artist'] = ','.join(args.artist)
if args.anime:
    parameters['cat'] = '5070'
elif args.comics:
    parameters['cat'] = '7030'
elif args.category:
    parameters['cat'] = args.category

#~~~ Initialize Curses ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
totalscreen = init_screen()
(headerwin, mainwin, footerwin) = divide_screen(totalscreen)
if args.browse:
    choose_category()

#~~~ Retrieve Results ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
fetch = FetchQueryThread(serverurl, parameters, args.offset, args.limit, pgsize)
fetch.start()

listwin = NzbHeaderListWindow(mainwin, displaylist)
listwin.draw_window()

query_string = serverurl + '/api?' + urlencode(parameters)
write_footer(query_string)
headerwin.addstr(0, 0, "~~~ Please Wait ", curses.color_pair(2))
while fetch.is_alive():
    if headerwin.getyx()[1] < (headerwin.getmaxyx()[1] - 1):
        headerwin.echochar('.', curses.color_pair(2))
        sleep(.25)

#~~~ Process Results ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
if not fetch.success:
    demolish_screen(totalscreen)
    print(fetch.error, file=sys.stderr)
    exit(1)

results = fetch.results
if len(results) == 0:
    demolish_screen(totalscreen)
    print("No Results.")
    exit(0)
if args.alpha:
    results.sort(key=lambda i: i['title'])
if args.reverse:
    results.reverse()

for i in range(len(results)):
    item = results[i]
    itemstrings = ['' for i in range(5)]
    itemstrings[0] = "{:>04d}".format(i+1)                                          # index
    itemstrings[1] = ' '                                                            # status
    itemstrings[2] = item['title'].replace('&amp;', '&')                            # title
    itemstrings[3] = "{:^22}".format(' '.join(item['pubDate'].split(' ')[1:-1]))    # date
    itemstrings[4] = "{:>10}".format(human(float(item['size'])))                    # size
    displaylist.append(itemstrings)

write_header("{:03d} Results returned".format(len(results)), 0)
write_status(' '.join(args.query))

listwin.new_data(displaylist)
listwin.draw_list()
write_status(f"{serverurl}:  {args.query}")
#write_status("DrawBorder: "+str(listwin.drawborder)+"   LineCount: "+str(listwin.line_count)+"  dy, dx: "+str(listwin.dy)+", "+str(listwin.dx))
write_footer("Press 'Q' to quit,  'Space' to queue,  'Enter' to retrieve")
curses.doupdate()

totalscreen.nodelay(False)
queuemon = threading.Thread(target = monitor_display_queue)
queuemon.start()

#~~~ Input Loop ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
while True:
    key = totalscreen.getch()
    if key == curses.ERR:
        continue
    handled = listwin.keypress(key)
    if not handled:
        if key in (ord('q'), ord('Q')):
            choose_to_exit()

        elif key == ord(' '):
            count = 0
            total = 0
            for idx in range(len(results)):
                if listwin.selected[idx]:
                    count += 1
                    total += results[idx]['size']
            displayqueue.put((write_status, (f'{count} items queued.  Total size:  {human(total)}',)))

        elif key in (ord('\n'), curses.KEY_ENTER):
            nzbfetch = threading.Thread(target=dispatch_fetch)
            nzbfetch.start()

        elif key == curses.KEY_RESIZE:
            (headerwin, mainwin, footerwin) = divide_screen(totalscreen)
            listwin.colwidths = [4, 1, 0, 22, 11]
            listwin.draw_window()

        elif key in (ord('r'), ord('R')):
            listwin.draw_list()
