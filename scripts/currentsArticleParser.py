import bs4
from bs4 import BeautifulSoup
from unidecode import unidecode
from PIL import Image
import re
import datetime
import requests
import pprint
import curses
import time
import urllib
import cStringIO
from urlparse import urljoin
from tidylib import tidy_fragment
import traceback
from itertools import tee, islice, chain, izip


def previous_and_next(some_iterable):
    prevs, items, nexts = tee(some_iterable, 3)
    prevs = chain([None], prevs)
    nexts = chain(islice(nexts, 1, None), [None])
    return izip(prevs, items, nexts)


class NoArticleBodyException(Exception):
    """
    Exception for when an article doesn't have a div of class storytext, and therefore
    is incorrectly formatted to be parsed by this application
    """
    def __init__(self):
        Exception.__init__(self, "Could not find a div of class storytext")


class InvalidDateException(Exception):
    """
    Exception for when an article doesn't have a div of class storytext, and therefore
    is incorrectly formatted to be parsed by this application
    """
    def __init__(self):
        Exception.__init__(self, "No Valid Date could be found for this article")


class ContentNotHTMLException(Exception):
    """
    Exception for when a url doesn't return html content
    """
    def __init__(self):
        Exception.__init__(self, "Content type not text/html; charset=UTF-8")


class GremlinZapper(object):
    """
    Class to convert windows cp1252 characters to unicode characters or
    to convert cp1252 and unicode characters to their ascii equivalents
    """

    def __init__(self):
        self.gremlin_regex_1252 = re.compile(r"[\x80-\x9f]")
        """ From http://effbot.org/zone/unicode-gremlins.htm """
        self.cp1252 = {
            # from http://www.microsoft.com/typography/unicode/1252.htm
            u"\x80": u"\u20AC",  # EURO SIGN
            u"\x82": u"\u201A",  # SINGLE LOW-9 QUOTATION MARK
            u"\x83": u"\u0192",  # LATIN SMALL LETTER F WITH HOOK
            u"\x84": u"\u201E",  # DOUBLE LOW-9 QUOTATION MARK
            u"\x85": u"\u2026",  # HORIZONTAL ELLIPSIS
            u"\x86": u"\u2020",  # DAGGER
            u"\x87": u"\u2021",  # DOUBLE DAGGER
            u"\x88": u"\u02C6",  # MODIFIER LETTER CIRCUMFLEX ACCENT
            u"\x89": u"\u2030",  # PER MILLE SIGN
            u"\x8A": u"\u0160",  # LATIN CAPITAL LETTER S WITH CARON
            u"\x8B": u"\u2039",  # SINGLE LEFT-POINTING ANGLE QUOTATION MARK
            u"\x8C": u"\u0152",  # LATIN CAPITAL LIGATURE OE
            u"\x8E": u"\u017D",  # LATIN CAPITAL LETTER Z WITH CARON
            u"\x91": u"\u2018",  # LEFT SINGLE QUOTATION MARK
            u"\x92": u"\u2019",  # RIGHT SINGLE QUOTATION MARK
            u"\x93": u"\u201C",  # LEFT DOUBLE QUOTATION MARK
            u"\x94": u"\u201D",  # RIGHT DOUBLE QUOTATION MARK
            u"\x95": u"\u2022",  # BULLET
            u"\x96": u"\u2013",  # EN DASH
            u"\x97": u"\u2014",  # EM DASH
            u"\x98": u"\u02DC",  # SMALL TILDE
            u"\x99": u"\u2122",  # TRADE MARK SIGN
            u"\x9A": u"\u0161",  # LATIN SMALL LETTER S WITH CARON
            u"\x9B": u"\u203A",  # SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
            u"\x9C": u"\u0153",  # LATIN SMALL LIGATURE OE
            u"\x9E": u"\u017E",  # LATIN SMALL LETTER Z WITH CARON
            u"\x9F": u"\u0178",  # LATIN CAPITAL LETTER Y WITH DIAERESIS
        }

    def kill_gremlins(self, text):
        """
        From http://effbot.org/zone/unicode-gremlins.htm
        map cp1252 gremlins to real unicode characters
        :return:
        """

        if re.search(u"[\x80-\x9f]", text):
            def fixup(m):
                s = m.group(0)
                return self.cp1252.get(s, s)

            if isinstance(text, type("")):
                # make sure we have a unicode string
                text = unicode(text, "iso-8859-1")
            text = re.sub(self.gremlin_regex_1252, fixup, text)
        return text

    def zap_string(self, the_string):
        """
        Converts any Windows cp1252 or unicode characters in a string to ASCII equivalents
        :param the_string: the string to perform the conversion on
        :return: input string with gremlins replaced
        """
        the_string = self.kill_gremlins(the_string)
        if isinstance(the_string, unicode):
            the_string = unidecode(the_string)
        return the_string


class CurrentsArticleParser(object):
    """
    Class to parse UCSC currents magazine articles and convert to markdown with yaml metadata
    """

    def __init__(self):
        """
        Pre-compiles all the regexes used for scraping in order to save time during execution
        :return:
        """
        self.date_regex = re.compile(r"[A-Za-z]+\s*\d{1,2}\,\s*\d{4}")
        self.end_story_regex = re.compile(r"\s*END\s*STORY\s*")
        self.word_regex = re.compile(r"([^\s\n\r\t]+)")
        self.article_slug_regex = re.compile(r".*\/([^\/\.]+)(?:.[^\.\/]+$)*")
        self.article_ending_regex = re.compile(r".*\/([^\/]+)")
        self.date_from_url_regex = re.compile(r"http://www1\.ucsc\.edu/currents/(\d+)-(\d+)/(\d+)-(\d+)/")
        self.years_from_url_regex = re.compile(r"http://www1\.ucsc\.edu/currents/(\d+)-(\d+)/")
        self.author_regex = re.compile(r"^[Bb]y\s+((?:\w+\s*){2,3})$")
        self.whitespace_regex = re.compile(r"^\s*$")
        self.object_index = 0

    def get_next_index(self):
        """
        Used as a counter to give each item (posts, images, and videos) a unique ID
        :return: the next unique id
        """
        self.object_index += 1
        return self.object_index

    def get_image_dimens(self, image_url):
        """
        Uses the PIL Pillow fork to get the width and height of an image from a url
        :param image_url: the url of the image to get the dimensions for
        :return: height, width
        """
        url_connection = urllib.urlopen(image_url)
        image_file = cStringIO.StringIO(url_connection.read())
        im = Image.open(image_file)
        return im.size

    def zap_tag_contents(self, tag):
        """
        Converts any Windows cp1252 or unicode characters in the text of
        a BeautifulSoup bs4.element.Tag Object to ASCII equivalents
        :rtype: bs4.element.Tag
        :param tag: the Tag object to convert
        :return: None
        """
        if hasattr(tag, 'contents'):
            content_length = len(tag.contents)

            gzapper = GremlinZapper()

            for x in range(0, content_length):
                if isinstance(tag.contents[x], bs4.element.NavigableString):
                    unicode_entry = gzapper.kill_gremlins(tag.contents[x])
                    unicode_entry = unidecode(unicode_entry)
                    tag.contents[x].replace_with(unicode_entry)
                elif isinstance(tag.contents[x], bs4.element.Tag):
                    self.zap_tag_contents(tag.contents[x])

    def get_soup_from_url(self, page_url):
        """
        Takes the url of a web page and returns a BeautifulSoup Soup object representation
        :param page_url: the url of the page to be parsed
        :param article_url: the url of the web page
        :raises: r.raise_for_status: if the url doesn't return an HTTP 200 response
        :return: A Soup object representing the page html
        """
        r = requests.get(page_url)
        if r.status_code != requests.codes.ok:
            r.raise_for_status()
        if r.headers['content-type'] != 'text/html; charset=UTF-8':
            raise ContentNotHTMLException
        return BeautifulSoup(r.content, 'lxml')

    def get_url_slug(self, page_url):
        """
        Returns the last section of a url eg. 'posts' for 'wordpress.com/posts.html'
        :raises Exception: if the regex is unable to locate the url slug
        :param page_url: the page url
        :return: the url slug
        """
        slug_match = self.article_slug_regex.findall(page_url)
        if slug_match and len(slug_match) == 1:
            return slug_match[0]
        else:
            raise Exception("unable to find slug for article: " + page_url + "\n")

    def get_url_ending(self, page_url):
        """
        Gets the url slug plus the file ending eg:
        www.example.com/example.html -> example.html
        :param page_url: the url to get the ending from
        :return: the url ending
        """
        slug_match = self.article_ending_regex.findall(page_url)
        if slug_match and len(slug_match) == 1:
            return slug_match[0]
        else:
            raise Exception("unable to find ending for article: " + page_url + "\n")

    def get_date_from_url(self, page_url):
        """
        Makes a guess as to the date of an article based off of its URL
        :param page_url:
        :return:
        """
        date = None
        date_matches = self.date_from_url_regex.findall(page_url)
        if date_matches:
            date_matches_tuple = date_matches[0]
            year0 = date_matches_tuple[0]
            year1 = date_matches_tuple[1]
            month = date_matches_tuple[2]
            month_as_int = int(date_matches_tuple[2])
            day = date_matches_tuple[3]

            # add the first two digits of the year
            if int(date_matches_tuple[0]) > 20:
                year0 = '19' + str(year0)
            else:
                year0 = '20' + str(year0)

            if int(date_matches_tuple[1]) > 20:
                year1 = '19' + str(year1)
            else:
                year1 = '20' + str(year1)

            if month_as_int > 6:
                date = year0 + '-' + month + '-' + day
            else:
                date = year1 + '-' + month + '-' + day
        else:
            date_matches = self.years_from_url_regex.findall(page_url)
            date_matches_tuple = date_matches[0]
            year0 = date_matches_tuple[0]
            if int(date_matches_tuple[0]) > 20:
                date = '19' + str(year0)
            else:
                date = '20' + str(year0)

        return date

    def html_to_markdown(self, html_string):
        """
        converts a string of html text to markdown using heckyesmarkdown.com
        :param html_string:
        :return:
        """
        r = requests.post('http://heckyesmarkdown.com/go/#sthash.Xf1YNf4U.dpuf',
                          data={'html': html_string, })

        if r.status_code != requests.codes.ok:
            r.raise_for_status()

        return r.text

    def get_images_storytext(self, story_text, article_url):
        """
        Extracts image information from the tables and returns a dictionary dictionaries of said information
        of the form:
        - image url:
            - image description
            - image width
            - image height
            - image id
        works for articles without a 'storytext' div, but not articles with one, as image descriptions
        are in the same cell for tables in the 'storytext' div, and in separate cells for tables that aren't.
        Removes the tables from the BeautifulSoup object representing the article body when done
        :param story_text: the BeautifulSoup object representing the article body
        :param article_url: the url of the article that images are being extracted from
        :return: article body without image tables, images_dictionary
        """
        images_dictionary = dict()
        gremlin_zapper = GremlinZapper()

        tables = story_text.findAll('table')

        for table in tables:
            # print item.parent
            add_to_story = False
            cells = table.find_all('td')

            if cells:
                add_to_story = False

                for cell in cells:
                    image = cell.find('img')
                    if image:
                        image_src = image['src']
                        image_src = urljoin(article_url, image_src)
                        try:
                            image_width, image_height = self.get_image_dimens(image_src)
                            if 'height' in image:
                                image_height = image['height']
                            if 'width' in image:
                                image_width = image['width']
                            image_text = cell.get_text()
                            matches = self.word_regex.findall(image_text)
                            image_text = ' '.join(matches)
                            image_text = gremlin_zapper.zap_string(image_text)
                            images_dictionary[image_src] = {"image_text": image_text,
                                                            "image_height": str(image_height),
                                                            "image_width": str(image_width),
                                                            "image_id": str(self.get_next_index())}
                        except IOError, urllib.URLError:
                            if 'height' in image and 'width' in image:
                                image_height = image['height']
                                image_width = image['width']
                                image_text = cell.get_text()
                                matches = self.word_regex.findall(image_text)
                                image_text = ' '.join(matches)
                                image_text = gremlin_zapper.zap_string(image_text)
                                images_dictionary[image_src] = {"image_text": image_text,
                                                                "image_height": str(image_height),
                                                                "image_width": str(image_width),
                                                                "image_id": str(self.get_next_index())}

            table.extract()
        return images_dictionary

    def get_images_no_storytext(self, article_body, article_url):
        """
        Extracts image information from the tables and returns a dictionary dictionaries of said information
        of the form:
        - image url:
            - image description
            - image width
            - image height
            - image id
        works for articles without a 'storytext' div, but not articles with one, as image descriptions
        are in the same cell for tables in the 'storytext' div, and in seperate cells for tables that aren't.
        Removes the tables from the BeautifulSoup object representing the article body when done
        :param article_body: the BeautifulSoup object representing the article body
        :param article_url: the url of the article that images are being extracted from
        :return: article body without image tables, images_dictionary
        """
        images_dictionary = dict()
        gremlin_zapper = GremlinZapper()

        tables = article_body.findAll('table')

        for table in tables:
            # print item.parent
            add_to_story = False
            cells = table.find_all('td')
            image_text = None
            image_src = None
            image_height = None
            image_width = None

            if cells:
                for cell in cells:

                    image = cell.find('img')
                    if image:
                        # print image['src']

                        if image_src is not None:
                            images_dictionary[image_src] = {"image_text": image_text,
                                                            "image_height": str(image_height),
                                                            "image_width": str(image_width),
                                                            "image_id": str(self.get_next_index())}
                            image_src = image['src']
                            image_src = urljoin(article_url, image_src)
                            try:
                                image_width, image_height = self.get_image_dimens(image_src)

                                if 'height' in image:
                                    image_height = image['height']
                                if 'width' in image:
                                    image_width = image['width']

                                image_text = None
                            except IOError, urllib.URLError:
                                if 'height' in image and 'width' in image:
                                    image_height = image['height']
                                    image_width = image['width']
                                    image_text = None
                                else:
                                    image_width = None
                                    image_height = None
                                    image_text = None
                                    image_src = None
                        else:
                            image_src = image['src']
                            image_src = urljoin(article_url, image_src)
                            try:
                                image_width, image_height = self.get_image_dimens(image_src)

                                if 'height' in image:
                                    image_height = image['height']
                                if 'width' in image:
                                    image_width = image['width']
                            except IOError, urllib.URLError:
                                if 'height' in image and 'width' in image:
                                    image_height = image['height']
                                    image_width = image['width']
                                    image_text = None
                                else:
                                    image_width = None
                                    image_height = None
                                    image_text = None
                                    image_src = None

                    else:
                        image_text = cell.get_text()
                        matches = self.word_regex.findall(image_text)
                        image_text = ' '.join(matches)
                        image_text = gremlin_zapper.zap_string(image_text)

                if image_src is not None:
                    images_dictionary[image_src] = {"image_text": image_text,
                                                    "image_height": str(image_height),
                                                    "image_width": str(image_width),
                                                    "image_id": str(self.get_next_index())}
            table.extract()
        return images_dictionary

    def parse_story_text(self, story_text, article_url):
        """
        Parses an article from when content was not contained within a 'storytext' div.
        Used for ucsc news articles from roughly 1998-2002.  Takes a BeautifulSoup object
        representing the 'storytext' div and scrapes it for article content
        :param story_text: A beautifulSoup object representing the storytext div
        :param article_url: The url of the article that is currently being scraped
        :return: A dictionary containing information about the article
                    - title
                    - author
                    - date
                    - post id
                    - a dictionary of information about any images in the article:
                        for each image url it contains
                            - height of the image
                            - width of the image
                            - the id of the image
                            - the description of the image
                    - and the html of the story body
                and returns it in dictionary form
        """

        title = None
        author = None
        date = None
        story_string = None
        gremlin_zapper = GremlinZapper()

        images_dictionary = self.get_images_storytext(story_text, article_url)

        for item in story_text.contents:
            # print type(item)
            add_to_story = True

            if isinstance(item, bs4.element.Tag):
                # print item
                if 'class' in item.attrs:
                    classes = item['class']
                    for the_class in classes:
                        if the_class == 'storyhead':
                            title = item.get_text()
                            matches = self.word_regex.findall(title)
                            title = ' '.join(matches)
                            title = gremlin_zapper.zap_string(title)
                            add_to_story = False
                        if the_class == 'subhead' and title is None:
                            title = item.get_text()
                            matches = self.word_regex.findall(title)
                            title = ' '.join(matches)
                            title = gremlin_zapper.zap_string(title)
                            add_to_story = False
                elif item.string:
                    match = self.date_regex.match(item.string)
                    if match:
                        # Convert date from Month, Day Year to Year-Month-Day
                        try:
                            raw_date = item.string
                            raw_date = raw_date.rstrip()
                            raw_date = raw_date.lstrip()
                            date = datetime.datetime.strptime(raw_date, "%B %d, %Y").strftime("%Y-%m-%d")
                            add_to_story = False
                        except ValueError:
                            add_to_story = True

                else:
                    story_end = False

                    if item.contents:
                        if len(item.contents) >= 1 and isinstance(item.contents[0], bs4.element.NavigableString):

                            author_matches = self.author_regex.findall(item.contents[0])
                            if author_matches:
                                author = author_matches[0]

                        if len(item.contents) >= 2:

                            if isinstance(item.contents[0], bs4.element.NavigableString) \
                                    and isinstance(item.contents[1], bs4.element.Tag) \
                                    and item.contents[1].name == 'a':
                                stripped = item.contents[0].rstrip().lstrip().lower()
                                if stripped == 'by':
                                    try:
                                        author = item.contents[1].string
                                        author = gremlin_zapper.zap_string(author)
                                    except TypeError:
                                        author = str(item.contents[1])
                                        matches = self.word_regex.findall(author)
                                        author = ' '.join(matches)
                                        author = gremlin_zapper.zap_string(author)
                                        soup = BeautifulSoup(author, 'lxml')
                                        author = soup.a.get_text()
                                    add_to_story = False

                        for cont in item.contents:
                            if isinstance(cont, bs4.element.Comment):
                                match = self.end_story_regex.match(cont.string)
                                if match:
                                    story_end = True
                            if isinstance(cont, bs4.element.NavigableString):
                                match = self.date_regex.findall(cont.string)
                                if match:
                                    # Convert date from Month, Day Year to Year-Month-Day
                                    try:
                                        raw_date = match[0]
                                        raw_date = raw_date.rstrip()
                                        raw_date = raw_date.lstrip()
                                        if date is None:
                                            date = datetime.datetime.strptime(raw_date, "%B %d, %Y")\
                                                .strftime("%Y-%m-%d")
                                    except ValueError:
                                        raw_date = None
                    if story_end:
                        break
            else:
                add_to_story = False

            if add_to_story:
                self.zap_tag_contents(item)

                if story_string is None:
                    story_string = str(item)
                else:
                    story_string += str(item)

        if author is None:
            author = "Public Information Department"

        return {'title': title,
                'author': author,
                'images_dictionary': images_dictionary,
                'article_body': story_string,
                'date': date,
                "post_id": str(self.get_next_index())}

    def parse_no_storytext_div(self, article_body, article_url):
        """
        Parses an article from before page content was contained within a 'storytext' div.
        Used for ucsc news articles from roughly 1998-2002.  These articles used tables,
        and page content was contained within a specific cell in the table.  Takes this cell
        as input instead of a div, and scrapes content from it
        :param article_body: A beautifulSoup object representing a cell in the page content table
                                containing the article content
        :param article_url: The url of the article that is currently being scraped
        :return: A dictionary containing information about the article
                    - title
                    - author
                    - date
                    - post id
                    - a dictionary of information about any images in the article:
                        for each image url it contains
                            - height of the image
                            - width of the image
                            - the id of the image
                            - the description of the image
                    - and the html of the story body
                and returns it in dictionary form
        """

        gremlin_zapper = GremlinZapper()
        title = None
        date = None
        author = None
        story_string = None

        # Extract all image information from the article before parsing it further
        # image information is contained within a table in the article
        images_dictionary = self.get_images_no_storytext(article_body, article_url)

        # now process the article for the rest of the information
        for item in article_body.contents:

            add_to_story = True
            author_paragraph = False
            paragraph_buffer = None
            # print type(item)
            if isinstance(item, bs4.element.Comment):
                add_to_story = False
                if item.lstrip().rstrip() == 'END PAGE CONTENT':
                    # print "END PAGE CONTENT"
                    break
            else:
                if isinstance(item, bs4.element.Tag):
                    if 'class' in item.attrs:
                        classes = item['class']
                        for the_class in classes:
                            if the_class == 'pageheadblack':
                                title = item.get_text()
                                matches = self.word_regex.findall(title)
                                title = ' '.join(matches)
                                title = gremlin_zapper.zap_string(title)
                                add_to_story = False
                    elif item.name == 'h1' or item.name == 'h2' or item.name == 'h3' or item.name == 'h4':
                        if title is None:
                            title = gremlin_zapper.zap_string(item.get_text())
                            add_to_story = False
                    elif item.string:
                        match = self.date_regex.match(item.string)
                        if match:
                            # Convert date from Month, Day Year to Year-Month-Day
                            try:
                                raw_date = item.string
                                raw_date = raw_date.rstrip()
                                raw_date = raw_date.lstrip()
                                date = datetime.datetime.strptime(raw_date, "%B %d, %Y").strftime("%Y-%m-%d")
                                add_to_story = False
                            except ValueError:
                                add_to_story = True
                    else:
                        story_end = False
                        if item.contents:
                            for previous, element, nxt in previous_and_next(item.contents):
                                if isinstance(element, bs4.element.NavigableString) \
                                        and isinstance(nxt, bs4.element.Tag) \
                                        and nxt.name == 'a' \
                                        and element.lstrip().rstrip().lower() == 'by':
                                    author_paragraph = True
                                    author = nxt.get_text()
                                    author = gremlin_zapper.zap_string(author)
                                elif isinstance(previous, bs4.element.NavigableString) \
                                        and isinstance(element, bs4.element.Tag) \
                                        and previous.lstrip().rstrip().lower() == 'by' \
                                        and element.name == 'a':
                                    author_paragraph = True
                                    author = element.get_text()
                                    author = gremlin_zapper.zap_string(author)
                                elif paragraph_buffer is None:
                                    if isinstance(element, bs4.element.Tag):
                                        if not (author_paragraph and element.name == 'br'):
                                            self.zap_tag_contents(element)
                                            paragraph_buffer = str(element)
                                    elif isinstance(element, bs4.element.NavigableString):
                                        paragraph_buffer = gremlin_zapper.zap_string(element)
                                else:
                                    if isinstance(element, bs4.element.Tag):
                                        if not (author_paragraph and element.name == 'br'):
                                            self.zap_tag_contents(element)
                                            paragraph_buffer += str(element)
                                    elif isinstance(element, bs4.element.NavigableString):
                                        paragraph_buffer += gremlin_zapper.zap_string(element)

                elif isinstance(item, bs4.element.NavigableString):
                    add_to_story = False
                    # print type(item)
                    zapped = gremlin_zapper.zap_string(item)
                    # print zapped
                    match = self.whitespace_regex.findall(zapped)
                    if not match:
                        if story_string is None:
                            story_string = zapped
                        else:
                            story_string += zapped
                else:
                    add_to_story = False
            if add_to_story:
                if author_paragraph:
                    if paragraph_buffer is None:
                        item = None
                    else:
                        item = "<p>" + paragraph_buffer + "</p>"
                else:
                    self.zap_tag_contents(item)

                if story_string is None and item is not None:
                    story_string = str(item)
                elif item is not None:
                    story_string += str(item)

        if author is None:
            author = "Public Information Office"

        return {'title': title,
                'author': author,
                'images_dictionary': images_dictionary,
                'article_body': story_string,
                'date': date,
                "post_id": str(self.get_next_index())}

    def scrape_article(self, article_url, diagnostic=False):
        """
        Gets HTML for a UCSC Currents online magazine article url, attempts to find:
            - title
            - author
            - date published
            - image links and captions (dictionary format ie: {img_link1: caption1, img_link2: caption2}
            - article body
        converts the article body to Markdown (https://daringfireball.net/projects/markdown/)
        then returns a dictionary of the above values

        :param article_url: the url to a UCSC Currents online magazine article
        :param diagnostic: Boolean value where if true, some cleanup work is skipped in
                            favor of faster run times
        :return: a dictionary of scraped values
        """

        soup = self.get_soup_from_url(article_url)

        # get the url slug for the new file name

        slug = self.get_url_slug(article_url)

        # this is the div that will hold any relevant article information
        story_text = soup.find('div', class_='storytext')

        article_dict = dict()

        # if there is no storytext div, then we either have a page that is an article
        # from 1998-2002 or a page that isn't scrapeable.  Attempt to find the correct cell
        # in the table layout for 1998-2002 news pages and use it to scrape the article.
        if story_text is None:
            table = soup.find('table')
            article_body = None
            if table:
                tr = table.find('tr', align='LEFT', valign='TOP')
                if tr:
                    tds = tr.findAll('td')
                    if tds and len(tds) > 1:
                        article_body = tds[1]
                    else:
                        raise NoArticleBodyException()
                else:
                    raise NoArticleBodyException()
            else:
                raise NoArticleBodyException()

            if article_body is None:
                raise NoArticleBodyException()
            else:
                article_dict = self.parse_no_storytext_div(article_body, article_url)
        else:
            article_dict = self.parse_story_text(story_text, article_url)

        if article_dict['date'] is None:
            date = self.get_date_from_url(article_url)
        else:
            date = article_dict['date']

        article_body = article_dict['article_body'] or ''

        if article_dict['title'] is None:
            article_dict['title'] = slug

        article_dict['date'] = date
        article_dict['file_name'] = date + '-' + slug + ".md"
        article_dict['source_permalink'] = "[source](" + article_url + " \"Permalink to " + slug + "\")"

        if diagnostic is False:
            document, errors = tidy_fragment(article_body, options={'numeric-entities': 1})
            article_dict['article_body'] = document

        return article_dict

    def write_article(self, article_dict):
        """
        Given a dictionary of article values:
        creates a new file in the current directory with title, author, date, and images in YAML format metadata
        followed by the Markdown format article body
        and finally a permalink to the article source link

        currently overwrites existing files if generated filenames are the same

        :param article_dict: A dictionary of scraped values for a UCSC Currents online magazine article
        :return None
        """

        title = article_dict['title'] or ''
        title = title.replace('"', "'")
        author = article_dict['author'] or ''
        post_id = article_dict['post_id']
        raw_date = article_dict['date']

        # Attempts to format the date correctly in order to predict urls for media urls uploaded to
        # a locally hosted wordpress site.  If this can't be done, it means that the parser was
        # unable to find an exact date for the article.  This means that the article will be
        # ignored by Jekyll's import process, and it is therefore pointless to write it to a file
        try:
            formatted_date = datetime.datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y/%m/")
        except ValueError:
            raise InvalidDateException

        fo = open(article_dict['file_name'], "w")
        fo.write("---\n")
        fo.write("layout: post\n")
        fo.write("title: \"" + title + "\"\n")
        fo.write("author: " + author + "\n")
        fo.write("post_id: " + post_id + "\n")
        fo.write("images:\n")

        for key in article_dict['images_dictionary']:
            fo.write("  - file: " + key + "\n")

            values_dict = article_dict['images_dictionary'][key]
            image_id = values_dict['image_id']

            fo.write('    image_id: ' + image_id + '\n')
            if values_dict['image_text'] is not None:
                replaced = values_dict['image_text'].replace('"', "'")
                fo.write("    caption: \"" + replaced + "\"\n")
            else:
                fo.write("    caption: \n")

        fo.write("---\n\n")

        for image_url in article_dict['images_dictionary']:
            values_dict = article_dict['images_dictionary'][image_url]

            image_text = values_dict['image_text'] or ""
            image_width = values_dict['image_width']
            image_height = values_dict['image_height']
            image_id = values_dict['image_id']

            url_ending = self.get_url_ending(image_url)

            fo.write("[caption id=\"attachment_" +
                     image_id + "\" align=\"alignright\" width=\"" + image_width +
                     "\"]<a href=\"http://localhost/mysite/wp-content/uploads/" +
                     formatted_date + url_ending + "\">"
                     "<img class=\"size-full wp-image-" + image_id + "\" "
                     "src=\"http://localhost/mysite/wp-content/uploads/" +
                     formatted_date + url_ending +
                     "\" alt=\"" + image_text + "\" width=\"" + image_width +
                     "\" height=\"" + image_height + "\" /></a>" + image_text +
                     "[/caption]\n")

        fo.write(article_dict['article_body'])
        fo.write("\n")
        fo.write(article_dict['source_permalink'] + "\n")
        fo.close()

    def report_progress(self, stdscr, url, progress_percent):
        """
        Updates progress bar for parse_articles

        :param stdscr: the terminal screen object to write to
        :param url: the url currently being processed
        :param progress_percent: the percentage of articles that has been processed
        :return:
        """
        stdscr.addstr(0, 0, "Total progress: [{1:50}] {0}%".format(progress_percent, "#" * (progress_percent / 2)))
        stdscr.move(1, 0)
        stdscr.clrtoeol()
        stdscr.refresh()
        stdscr.addstr(1, 0, "Analyzing URL: {0}".format(url))
        stdscr.addstr(2, 0, "")
        stdscr.refresh()

    def parse_articles(self, article_url_list):
        """
        Scrapes and writes each article in the given list, and collects and returns
        information about the scrapeability of each article in the list
        :param article_url_list:
        :return: a dictionary containing the number of articles and several lists
                    of articles that correspond in to the category described by
                    the list name
        """

        # initiate the command line for the parsing visualization
        stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()

        num_urls = len(article_url_list)
        current_url_num = 1
        prog_percent = 0

        missing_title = []
        missing_author = []
        missing_date = []

        missing_author_title = []
        missing_author_date = []
        missing_title_date = []

        missing_title_author_date = []

        not_article = []

        scrapable_urls = []
        unscrapable_urls = []
        partially_scrapable_urls = []

        for article_url in article_url_list:
            article_url = article_url.rstrip()
            # print article_url
            self.report_progress(stdscr, article_url, prog_percent)
            try:
                article_dictionary = self.scrape_article(article_url, diagnostic=False)
                self.write_article(article_dictionary)
                has_title = article_dictionary['title'] is not None
                has_author = article_dictionary['author'] is not None
                has_date = article_dictionary['date'] is not None

                # print has_title, has_author, has_date

                if has_author and has_date and has_title:
                    scrapable_urls.append(article_url)
                    # print "completeley scrapable:" + article_url
                elif not has_author and not has_date and not has_title:
                    # print "unscrapeable: " + article_url
                    partially_scrapable_urls.append(article_url)
                    missing_title_author_date.append(article_url)
                else:
                    # print "partially scrapeable: " + article_url
                    partially_scrapable_urls.append(article_url)

                    if not has_author and has_title and has_date:
                        missing_author.append(article_url)

                    if has_author and not has_title and has_date:
                        missing_title.append(article_url)

                    if has_author and has_title and not has_date:
                        missing_date.append(article_url)

                    if not has_author and not has_title and has_date:
                        missing_author_title.append(article_url)

                    if not has_author and has_title and not has_date:
                        missing_author_date.append(article_url)

                    if has_author and not has_title and not has_date:
                        missing_title_date.append(article_url)

            except NoArticleBodyException:
                unscrapable_urls.append(article_url)
            except requests.exceptions.HTTPError:
                unscrapable_urls.append(article_url)
            except requests.exceptions.ConnectionError:
                unscrapable_urls.append(article_url)
            except ContentNotHTMLException:
                not_article.append(article_url)
            except InvalidDateException:
                unscrapable_urls.append(article_url)
            except Exception as e:
                curses.echo()
                curses.nocbreak()
                curses.endwin()
                traceback.print_exc()
                print str(e)
                print article_url
                exit()

            prog_percent = int(((current_url_num + 0.0) / num_urls) * 100)
            current_url_num += 1

        # end curses session
        curses.echo()
        curses.nocbreak()
        curses.endwin()

        return {
            'num_urls': num_urls,
            'missing_title': missing_title,
            'missing_author': missing_author,
            'missing_date': missing_date,
            'missing_author_title': missing_author_title,
            'missing_author_date': missing_author_date,
            'missing_title_date': missing_title_date,
            'missing_title_author_date': missing_title_author_date,
            'not_article': not_article,
            'scrapable_urls': scrapable_urls,
            'unscrapable_urls': unscrapable_urls,
            'partially_scrapable_urls': partially_scrapable_urls,
        }

    def generate_scrapeability_report(self, scrapeability_dictionary):
        """
        Uses the information in the scrapeability dictionary created by the function
        'parse_articles' to calculate scrapeability statistics and write a report to a file
        called 'scrapeability_report.txt'

        :param scrapeability_dictionary: The dictionary returned by 'parse_articles'
        :return:
        """
        num_urls = scrapeability_dictionary['num_urls']
        missing_title = scrapeability_dictionary['missing_title']
        missing_author = scrapeability_dictionary['missing_author']
        missing_date = scrapeability_dictionary['missing_date']

        missing_author_title = scrapeability_dictionary['missing_author_title']
        missing_author_date = scrapeability_dictionary['missing_author_date']
        missing_title_date = scrapeability_dictionary['missing_title_date']

        missing_title_author_date = scrapeability_dictionary['missing_title_author_date']

        not_article = scrapeability_dictionary['not_article']

        scrapable_urls = scrapeability_dictionary['scrapable_urls']
        unscrapable_urls = scrapeability_dictionary['unscrapable_urls']
        partially_scrapable_urls = scrapeability_dictionary['partially_scrapable_urls']

        fo = open('scrapeability_report.txt', "w")

        # categories for all articles
        num_scrapable_urls = len(scrapable_urls)
        num_partially_scrapable_urls = len(partially_scrapable_urls)
        num_unscrapable_urls = len(unscrapable_urls)
        num_not_articles = len(not_article)

        percent_scrapable = ((num_scrapable_urls + 0.0) / num_urls) * 100
        percent_partially_scrapable = ((num_partially_scrapable_urls + 0.0) / num_urls) * 100
        percent_unscrapable = ((num_unscrapable_urls + 0.0) / num_urls) * 100
        percent_not_article = ((num_not_articles + 0.0) / num_urls) * 100

        num_possible_to_scrape = num_partially_scrapable_urls + num_scrapable_urls
        percent_possible_fully_scrapable = ((num_scrapable_urls + 0.0) / num_possible_to_scrape) * 100
        percent_possible_partially_scrapable = ((num_partially_scrapable_urls + 0.0) / num_possible_to_scrape) * 100

        fo.write("URL List Scrapeability Statistics\n\n")

        fo.write('of the ' + str(num_urls) + ' total urls,\n')
        fo.write('\t' + str(num_scrapable_urls) + ' (' + str(percent_scrapable) + '%) were completely scrapeable,\n')
        fo.write('\t' + str(num_partially_scrapable_urls) + ' (' + str(percent_partially_scrapable))
        fo.write('%) were partially scrapeable,\n')
        fo.write('\t' + str(num_unscrapable_urls) + ' (' + str(percent_unscrapable) + '%) were unscrapeable, and\n')
        fo.write('\t' + str(num_not_articles) + ' (' + str(percent_not_article) + '%) were not articles.\n\n\n')

        fo.write('of the ' + str(num_possible_to_scrape) + ' urls that were at least partially scrapable,\n')
        fo.write('\t' + str(num_scrapable_urls) + ' (' + str(percent_possible_fully_scrapable) +
                 '%) were completely scrapeable, and\n')
        fo.write('\t' + str(num_partially_scrapable_urls) + ' (' + str(percent_possible_partially_scrapable) +
                 '%) were partially scrapeable.\n\n\n')

        # categories for partially scrapable articles
        if num_partially_scrapable_urls > 0:
            num_one_missing = len(missing_author) + len(missing_title) + len(missing_date)
            num_two_missing = len(missing_title_date) + len(missing_author_date) + len(missing_author_title)
            num_three_missing = len(missing_title_author_date)

            percent_one_missing = ((num_one_missing + 0.0) / num_partially_scrapable_urls) * 100
            percent_two_missing = ((num_two_missing + 0.0) / num_partially_scrapable_urls) * 100
            percent_three_missing = ((num_three_missing + 0.0) / num_partially_scrapable_urls) * 100

            fo.write("of the " + str(num_partially_scrapable_urls) + " partially scrapeable urls,\n")
            fo.write('\t' + str(num_one_missing) + ' (' + str(percent_one_missing) + '%) were missing one attribute,\n')
            fo.write('\t' + str(num_two_missing) + ' (' + str(percent_two_missing) + '%) were missing two attributes, '
                                                                                     'and\n')
            fo.write('\t' + str(num_three_missing) + ' (' + str(percent_three_missing) +
                     '%) were missing three attributes')

            # categories for one_missing
            if num_one_missing > 0:
                percent_missing_author = ((len(missing_author) + 0.0) / num_one_missing) * 100
                percent_missing_title = ((len(missing_title) + 0.0) / num_one_missing) * 100
                percent_missing_date = ((len(missing_date) + 0.0) / num_one_missing) * 100

                fo.write("\n\n\nof the " + str(num_one_missing) + " articles missing one attribute,\n")
                fo.write('\t' + str(len(missing_author)) + ' (' + str(percent_missing_author) +
                         '%) were missing an author,\n')
                fo.write('\t' + str(len(missing_title)) + ' (' + str(percent_missing_title) +
                         '%) were missing a title, and\n')
                fo.write('\t' + str(len(missing_date)) + ' (' + str(percent_missing_date) +
                         '%) were missing a date\n')

            # categories for two_missing
            if num_two_missing:
                percent_missing_author_title = ((len(missing_author_title) + 0.0) / num_two_missing) * 100
                percent_missing_author_date = ((len(missing_author_date) + 0.0) / num_two_missing) * 100
                percent_missing_title_date = ((len(missing_title_date) + 0.0) / num_two_missing) * 100

                fo.write("\n\nof the " + str(num_two_missing) + " articles missing two attributes,\n")
                fo.write('\t' + str(len(missing_author_title)) + ' (' + str(percent_missing_author_title) +
                         '%) were missing an author and a title,\n')
                fo.write('\t' + str(len(missing_author_date)) + ' (' + str(percent_missing_author_date) +
                         '%) were missing an author and a date, and\n')
                fo.write('\t' + str(len(missing_title_date)) + ' (' + str(percent_missing_title_date) +
                         '%) were missing a title and a date\n')

        fo.write("\n\nURLs placed into relevant categories: \n\n")

        fo.write("\n\nLists of which articles are missing which attributes:\n\n")
        fo.write("Missing Author: " + str(len(missing_author)) + " articles\n")
        fo.write(pprint.pformat(missing_author, indent=4))
        fo.write("\n\nMissing Date: " + str(len(missing_date)) + " articles\n")
        fo.write(pprint.pformat(missing_date, indent=4))
        fo.write("\n\nMissing Title: " + str(len(missing_title)) + " articles\n")
        fo.write(pprint.pformat(missing_title, indent=4))
        fo.write("\n\nMissing Author and Title: " + str(len(missing_author_title)) + " articles\n")
        fo.write(pprint.pformat(missing_author_title, indent=4))
        fo.write("\n\nMissing Author and Date: " + str(len(missing_author_date)) + " articles\n")
        fo.write(pprint.pformat(missing_author_date, indent=4))
        fo.write("\n\nMissing Date and Title: " + str(len(missing_title_date)) + " articles\n")
        fo.write(pprint.pformat(missing_title_date, indent=4))
        fo.write("\n\nMissing Author and Date and Title: " + str(len(missing_title_author_date)) + " articles\n")
        fo.write(pprint.pformat(missing_title_author_date, indent=4))

        fo.write("\n\nUnscrapeable URLs:\n")
        fo.write(pprint.pformat(unscrapable_urls, indent=4))
        fo.write("\n\nPartially Scrapeable URLs:\n")
        fo.write(pprint.pformat(partially_scrapable_urls, indent=4))
        fo.write("\n\nCompleteley Scrapeable URLs:\n")
        fo.write(pprint.pformat(scrapable_urls, indent=4))

        fo.close()

    def run_parser(self, article_url_list):
        """
        Takes a list of urls, attempts to scrape and write them, keeps track of the whether each
        article was scraped succesfully, and if it was how successful the scrape was,
        then writes this diagnostic information to a file called 'scrapeability_report.txt'

        :param article_url_list: The list of pages to attempt to parse
        :return:
        """

        scrapeability_dict = self.parse_articles(article_url_list)

        print 'Generating Scrapeability Report...'

        self.generate_scrapeability_report(scrapeability_dict)

        print 'Done'

    def temp_driver(self, article_url):
        """
        Used for testing the parser on individual articles without the
        command line visualization
        :param article_url: the url of the article to scrape
        :return:
        """
        soup = self.get_soup_from_url(article_url)
        story_text = soup.find('div', class_='storytext')
        article_body = None
        article_dict = dict()

        if story_text is None:
            table = soup.find('table')
            article_body = None
            if table:
                tr = table.find('tr', align='LEFT', valign='TOP')
                if tr:
                    tds = tr.findAll('td')
                    if tds and len(tds) > 1:
                        article_body = tds[1]
                    else:
                        raise NoArticleBodyException()
                else:
                    raise NoArticleBodyException()
            else:
                raise NoArticleBodyException()

        vals_dict = self.scrape_article(article_url, diagnostic=False)

        self.write_article(vals_dict)