import copy
import gettext
import json
import logging
import os
import tempfile
import io
import time

import namedlist
import trollius
from trollius import From, Return

from wpull.backport.logging import BraceMessage as __
from wpull.document.html import HTMLReader
from wpull.body import Body
from wpull.driver.phantomjs import PhantomJSRPCError
from wpull.driver.resource import PhantomJSResourceTracker
from wpull.driver.scroller import Scroller
from wpull.http.request import Request, Response
from wpull.namevalue import NameValueRecord
from wpull.warc import WARCRecord
import wpull.url


PhantomJSParams = namedlist.namedtuple(
    'PhantomJSParamsType', [
        ('snapshot_types', ('html', 'pdf')),
        ('wait_time', 1),
        ('num_scrolls', 10),
        ('smart_scroll', True),
        ('snapshot', True),
        ('viewport_size', (1200, 1920)),
        ('paper_size', (2400, 3840))
    ]
)


_logger = logging.getLogger(__name__)
_ = gettext.gettext


class PhantomJSCoprocessor(object):
    '''PhantomJS coprocessor.'''
    def __init__(self, phantomjs_pool, processing_rule, statistics,
                 fetch_rule, result_rule, phantomjs_params,
                 warc_recorder=None, root_path='.'):
        self._phantomjs_pool = phantomjs_pool
        self._processing_rule = processing_rule
        self._statistics = statistics
        self._fetch_rule = fetch_rule
        self._result_rule = result_rule
        self._phantomjs_params = phantomjs_params
        self._warc_recorder = warc_recorder
        self._root_path = root_path

        self._file_writer_session = None

    @trollius.coroutine
    def process(self, url_item, request, response, file_writer_session):
        '''Process PhantomJS.

        Coroutine.
        '''
        if response.status_code != 200:
            return

        if not HTMLReader.is_supported(request=request, response=response):
            return

        # TODO: is page dynamic?

        _logger.debug('Starting PhantomJS processing.')

        self._file_writer_session = file_writer_session

        # FIXME: this is a quick hack for handling time outs. See #137.
        attempts = int(os.environ.get('WPULL_PHANTOMJS_TRIES', 5))

        for dummy in range(attempts):
            try:
                yield From(self._run_driver(url_item, request, response))
            except PhantomJSRPCError as error:
                _logger.exception(__('PhantomJS Error: {}', error))
            else:
                break
        else:
            _logger.warning(__(
                _('PhantomJS failed to fetch ‘{url}’. I am sorry.'),
                url=request.url_info.url
            ))

    @trollius.coroutine
    def _run_driver(self, url_item, request, response):
        _logger.debug('Started PhantomJS processing.')

        with self._phantomjs_pool.session() as driver:
            session = PhantomJSCoprocessorSession(
                driver, self._fetch_rule, self._result_rule,
                url_item,
                self._phantomjs_params, warc_recorder=self._warc_recorder
            )
            # TODO: need to implement and handle timeouts
            yield From(driver.start())
            yield From(session.fetch(request.url_info.url))
            yield From(session.wait_load())
            yield From(session.scroll_page())
            yield From(session.wait_load())
            yield From(self._take_snapshots(session))
            yield From(self._scrape_document(session, request, response, url_item))

        _logger.debug('Ended PhantomJS processing.')

    @trollius.coroutine
    def _take_snapshots(self, session, infix='snapshot'):
        for snapshot_type in self._phantomjs_params.snapshot_types or ():
            path = self._file_writer_session.extra_resource_path(
                '.{infix}.{file_type}'.format(infix=infix, file_type=snapshot_type)
            )

            if not path:
                temp_fd, temp_path = tempfile.mkstemp(
                        dir=self._root_path, prefix='phnsh',
                        suffix='.{}'.format(snapshot_type)
                    )
                os.close(temp_fd)
                path = temp_path
            else:
                temp_path = None

            try:
                yield From(session.take_snapshot(path))
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)

    @trollius.coroutine
    def _scrape_document(self, session, request, response, url_item):
        temp_fd, temp_path = tempfile.mkstemp(
            dir=self._root_path, prefix='phnsc',
            suffix='.html'
        )
        os.close(temp_fd)

        yield From(session.take_snapshot(temp_path, add_warc=False))

        mock_response = self._new_mock_response(response, temp_path)

        self._processing_rule.scrape_document(request, mock_response, url_item)

        if mock_response.body:
            mock_response.body.close()

        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

        _logger.debug('Ended PhantomJS processing.')

    def _new_mock_response(self, response, file_path):
        '''Return a new mock Response with the content.'''
        mock_response = copy.copy(response)

        mock_response.body = Body(open(file_path, 'rb'))
        mock_response.fields = NameValueRecord()

        for name, value in response.fields.get_all():
            mock_response.fields.add(name, value)

        mock_response.fields['Content-Type'] = 'text/html; charset="utf-8"'

        return mock_response


class PhantomJSCoprocessorSession(object):
    def __init__(self, driver, fetch_rule, result_rule, url_item, params, warc_recorder=None):
        self._driver = driver
        self._fetch_rule = fetch_rule
        self._result_rule = result_rule
        self._url_item = url_item
        self._params = params
        self._warc_recorder = warc_recorder

        self._resource_tracker = PhantomJSResourceTracker()
        self._scroller = Scroller(
            driver, self._resource_tracker,
            scroll_height=self._params.viewport_size[1],
            wait_time=self._params.wait_time,
            num_scrolls=self._params.num_scrolls,
            smart_scroll=self._params.smart_scroll
        )
        self._scroller.action_callback = self._log_action

        self._actions = []
        self._action_warc_record = None
        self._load_state = 'not_started'

        driver.page_event_handlers['load_started'] = self._load_started_cb
        driver.page_event_handlers['load_finished'] = self._load_finished_cb
        driver.page_event_handlers['resource_requested'] = self._resource_requested_cb
        driver.page_event_handlers['resource_received'] = self._resource_received_cb
        driver.page_event_handlers['resource_error'] = self._resource_error_cb
        driver.page_event_handlers['resource_timeout'] = self._resource_timeout_cb
        driver.page_event_handlers['error'] = self._error_cb

    @trollius.coroutine
    def fetch(self, url):
        _logger.info(__(
            _('PhantomJS fetching ‘{url}’.'),
            url=url
        ))

        if url.startswith('https://'):
            _logger.debug('Rewriting')
            url = 'http://{}/WPULLHTTPS'.format(url[8:])

        yield From(self._driver.open_page(
            url,
            viewport_size=self._params.viewport_size,
            paper_size=self._params.paper_size
        ))

    @trollius.coroutine
    def wait_load(self):
        _logger.debug('Wait load')

        # TODO: need a session timeout option
        while self._load_state != 'finished' or self._resource_tracker.pending:
            yield From(trollius.sleep(0.1))

        _logger.debug('Wait over')

    @trollius.coroutine
    def scroll_page(self):
        # Try to get rid of any stupid "sign up now" overlays.
        click_x, click_y = self._params.viewport_size
        self._log_action('click', [click_x, click_y])
        yield From(self._driver.send_click(click_x, click_y))

        yield From(self._scroller.scroll_to_bottom())

        if self._warc_recorder:
            url = yield From(self._driver.get_page_url())
            self._add_warc_action_log(url)

    def _log_action(self, name, value):
        '''Add a action to the action log.'''
        _logger.debug(__('Action: {0} {1}', name, value))

        self._actions.append({
            'event': name,
            'value': value,
            'timestamp': time.time(),
        })

    def _add_warc_action_log(self, url):
        '''Add the acton log to the WARC file.'''
        _logger.debug('Adding action log record.')

        log_data = json.dumps(
            {'actions': self._actions},
            indent=4,
            ).encode('utf-8')

        self._action_warc_record = record = WARCRecord()
        record.set_common_fields('metadata', 'application/json')
        record.fields['WARC-Target-URI'] = 'urn:X-wpull:snapshot?url={0}' \
            .format(wpull.url.percent_encode_query_value(url))
        record.block_file = io.BytesIO(log_data)

        self._warc_recorder.set_length_and_maybe_checksums(record)
        self._warc_recorder.write_record(record)

    @trollius.coroutine
    def take_snapshot(self, path, add_warc=True):
        extension = os.path.splitext(path)[1]

        assert extension in ('.pdf', '.png', '.html'), (path, extension)

        _logger.debug(__('Saving snapshot to {0}.', path))

        dir_path = os.path.abspath(os.path.dirname(path))

        if not os.path.exists(dir_path):
            os.makedirs(dir_path)

        yield From(self._scroller.scroll_to_top())
        yield From(self._driver.snapshot(path))
        url = yield From(self._driver.get_page_url())

        if self._warc_recorder and add_warc:
            mime_type = {
                '.pdf': 'application/pdf',
                '.html': 'text/html',
                '.png': 'image/png',
                }[extension]

            self._add_warc_snapshot(path, mime_type, url)

    def _add_warc_snapshot(self, filename, content_type, url):
        '''Add the snaphot to the WARC file.'''
        _logger.debug('Adding snapshot record.')

        record = WARCRecord()
        record.set_common_fields('resource', content_type)
        record.fields['WARC-Target-URI'] = 'urn:X-wpull:snapshot?url={0}' \
            .format(wpull.url.percent_encode_query_value(url))

        if self._action_warc_record:
            record.fields['WARC-Concurrent-To'] = \
                self._action_warc_record.fields[WARCRecord.WARC_RECORD_ID]

        with open(filename, 'rb') as in_file:
            record.block_file = in_file

            self._warc_recorder.set_length_and_maybe_checksums(record)
            self._warc_recorder.write_record(record)

    def _load_started_cb(self, message):
        _logger.debug('Load started.')
        self._load_state = 'started'

    def _load_finished_cb(self, message):
        _logger.debug('Load finished')
        self._load_state = 'finished'

    def _resource_requested_cb(self, message):
        request_data = message['request_data']
        _logger.debug(__('Resource requested {}', request_data['url']))

        url_info = wpull.url.parse_url_or_log(request_data['url'])

        if not url_info:
            return

        # FIXME: need to tell resource is inline
        should_fetch = self._fetch_rule.check_generic_request(
            url_info, self._url_item.url_record)[0]

        # if should_fetch:
        if True:
            url = request_data['url']

            _logger.info(__(
                _('PhantomJS fetching ‘{url}’.'),
                url=url
            ))

            self._resource_tracker.process_request(request_data)

            if url.startswith('https://'):
                _logger.debug('Rewriting')
                new_url = 'http://{}/WPULLHTTPS'.format(url[8:])
                return new_url

        else:
            _logger.debug('Aborting.')
            return 'abort'

    def _resource_received_cb(self, message):
        response = message['response']

        self._resource_tracker.process_response(response)

        if response['stage'] != 'end':
            return

        # TODO
        # self._result_rule.handle_document(adsfasdfa,asdfsdf)

        url = response['url']

        if url.endswith('/WPULLHTTPS'):
            url = url[:-11].replace('http://', 'https://', 1)

        _logger.info(__(
            _('PhantomJS fetched ‘{url}’: {status_code} {reason}. '
              'Length: {content_length} [{content_type}].'),
            url=url,
            status_code=response['status'],
            reason=response['statusText'],
            content_length=response.get('bodySize'),
            content_type=response.get('contentType'),
            ))

    def _resource_error_cb(self, message):
        resource_error = message['resource_error']

        self._resource_tracker.process_error(resource_error)

        _logger.error(__(
            _('PhantomJS fetching ‘{url}’ encountered an error: {error}'),
            url=resource_error['url'],
            error=resource_error['errorString']
        ))

        # TODO
        # self._result_rule.handle_error(asdfasdf, asdfsadfasdf)

    def _resource_timeout_cb(self, message):
        request = message['request']

        self._resource_tracker.process_error(request)

        _logger.error(__(
            _('PhantomJS fetching ‘{url}’ encountered an error: {error}'),
            url=request['url'],
            error=request['errorString']
        ))

        # TODO
        # self._result_rule.handle_error(asdfasdf, asdfsadfasdf)

    def _error_cb(self, message):
        _logger.error(__(
            _('PhantomJSError: {message} {trace}'),
            message=message['message'],
            trace=message['trace']
        ))


def convert_phantomjs_request(request_data):
    url_info = wpull.url.parse_url_or_log(request_data['url'])

    if not url_info:
        return

    request = Request()
    request.method = request_data['method']
    request.url_info = url_info

    for header in request_data['headers']:
        request.fields.add(header['name'], header['value'])

    return request


def convert_phantomjs_response(response):
    url_info = wpull.url.parse_url_or_log(response['url'])

    if not url_info:
        return

    response = Response(
        status_code=response['status'],
        reason=response['statusText'],
    )
    response.url_info = url_info

    for header in response['headers']:
        response.fields.add(header['name'], header['value'])

    return response
