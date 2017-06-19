# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2016-2017 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Wrapper over a QWebEngineView."""

import os
import functools

import sip
from PyQt5.QtCore import pyqtSlot, Qt, QEvent, QPoint, QUrl, QTimer
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtNetwork import QAuthenticator
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineScript

from qutebrowser.browser import browsertab, mouse, shared
from qutebrowser.browser.webengine import (webview, webengineelem, tabhistory,
                                           interceptor, webenginequtescheme,
                                           webenginedownloads,
                                           webenginesettings)
from qutebrowser.misc import miscwidgets
from qutebrowser.utils import (usertypes, qtutils, log, javascript, utils,
                               objreg, jinja, debug, version)


_qute_scheme_handler = None


def init():
    """Initialize QtWebEngine-specific modules."""
    # For some reason we need to keep a reference, otherwise the scheme handler
    # won't work...
    # https://www.riverbankcomputing.com/pipermail/pyqt/2016-September/038075.html
    global _qute_scheme_handler
    app = QApplication.instance()

    software_rendering = os.environ.get('LIBGL_ALWAYS_SOFTWARE') == '1'
    if version.opengl_vendor() == 'nouveau' and not software_rendering:
        # FIXME:qtwebengine display something more sophisticated here
        raise browsertab.WebTabError(
            "QtWebEngine is not supported with Nouveau graphics (unless "
            "LIBGL_ALWAYS_SOFTWARE is set as environment variable).")

    log.init.debug("Initializing qute://* handler...")
    _qute_scheme_handler = webenginequtescheme.QuteSchemeHandler(parent=app)
    _qute_scheme_handler.install(webenginesettings.default_profile)
    _qute_scheme_handler.install(webenginesettings.private_profile)

    log.init.debug("Initializing request interceptor...")
    host_blocker = objreg.get('host-blocker')
    req_interceptor = interceptor.RequestInterceptor(
        host_blocker, parent=app)
    req_interceptor.install(webenginesettings.default_profile)
    req_interceptor.install(webenginesettings.private_profile)

    log.init.debug("Initializing QtWebEngine downloads...")
    download_manager = webenginedownloads.DownloadManager(parent=app)
    download_manager.install(webenginesettings.default_profile)
    download_manager.install(webenginesettings.private_profile)
    objreg.register('webengine-download-manager', download_manager)


# Mapping worlds from usertypes.JsWorld to QWebEngineScript world IDs.
_JS_WORLD_MAP = {
    usertypes.JsWorld.main: QWebEngineScript.MainWorld,
    usertypes.JsWorld.application: QWebEngineScript.ApplicationWorld,
    usertypes.JsWorld.user: QWebEngineScript.UserWorld,
    usertypes.JsWorld.jseval: QWebEngineScript.UserWorld + 1,
}


class WebEngineAction(browsertab.AbstractAction):

    """QtWebEngine implementations related to web actions."""

    action_class = QWebEnginePage
    action_base = QWebEnginePage.WebAction

    def exit_fullscreen(self):
        self._widget.triggerPageAction(QWebEnginePage.ExitFullScreen)

    def save_page(self):
        """Save the current page."""
        self._widget.triggerPageAction(QWebEnginePage.SavePage)


class WebEnginePrinting(browsertab.AbstractPrinting):

    """QtWebEngine implementations related to printing."""

    def check_pdf_support(self):
        return True

    def check_printer_support(self):
        if not hasattr(self._widget.page(), 'print'):
            raise browsertab.WebTabError(
                "Printing is unsupported with QtWebEngine on Qt < 5.8")

    def check_preview_support(self):
        raise browsertab.WebTabError(
            "Print previews are unsupported with QtWebEngine")

    def to_pdf(self, filename):
        self._widget.page().printToPdf(filename)

    def to_printer(self, printer, callback=None):
        if callback is None:
            callback = lambda _ok: None
        self._widget.page().print(printer, callback)


class WebEngineSearch(browsertab.AbstractSearch):

    """QtWebEngine implementations related to searching on the page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flags = QWebEnginePage.FindFlags(0)

    def _find(self, text, flags, callback, caller):
        """Call findText on the widget."""
        self.search_displayed = True

        def wrapped_callback(found):
            """Wrap the callback to do debug logging."""
            found_text = 'found' if found else "didn't find"
            if flags:
                flag_text = 'with flags {}'.format(debug.qflags_key(
                    QWebEnginePage, flags, klass=QWebEnginePage.FindFlag))
            else:
                flag_text = ''
            log.webview.debug(' '.join([caller, found_text, text, flag_text])
                              .strip())
            if callback is not None:
                callback(found)
        self._widget.findText(text, flags, wrapped_callback)

    def search(self, text, *, ignore_case=False, reverse=False,
               result_cb=None):
        flags = QWebEnginePage.FindFlags(0)
        if ignore_case == 'smart':
            if not text.islower():
                flags |= QWebEnginePage.FindCaseSensitively
        elif not ignore_case:
            flags |= QWebEnginePage.FindCaseSensitively
        if reverse:
            flags |= QWebEnginePage.FindBackward

        self.text = text
        self._flags = flags
        self._find(text, flags, result_cb, 'search')

    def clear(self):
        self.search_displayed = False
        self._widget.findText('')

    def prev_result(self, *, result_cb=None):
        # The int() here makes sure we get a copy of the flags.
        flags = QWebEnginePage.FindFlags(int(self._flags))
        if flags & QWebEnginePage.FindBackward:
            flags &= ~QWebEnginePage.FindBackward
        else:
            flags |= QWebEnginePage.FindBackward
        self._find(self.text, flags, result_cb, 'prev_result')

    def next_result(self, *, result_cb=None):
        self._find(self.text, self._flags, result_cb, 'next_result')


class WebEngineCaret(browsertab.AbstractCaret):

    """QtWebEngine implementations related to moving the cursor/selection."""

    @pyqtSlot(usertypes.KeyMode)
    def _on_mode_entered(self, mode):
        pass

    @pyqtSlot(usertypes.KeyMode)
    def _on_mode_left(self):
        pass

    def move_to_next_line(self, count=1):
        log.stub()

    def move_to_prev_line(self, count=1):
        log.stub()

    def move_to_next_char(self, count=1):
        log.stub()

    def move_to_prev_char(self, count=1):
        log.stub()

    def move_to_end_of_word(self, count=1):
        log.stub()

    def move_to_next_word(self, count=1):
        log.stub()

    def move_to_prev_word(self, count=1):
        log.stub()

    def move_to_start_of_line(self):
        log.stub()

    def move_to_end_of_line(self):
        log.stub()

    def move_to_start_of_next_block(self, count=1):
        log.stub()

    def move_to_start_of_prev_block(self, count=1):
        log.stub()

    def move_to_end_of_next_block(self, count=1):
        log.stub()

    def move_to_end_of_prev_block(self, count=1):
        log.stub()

    def move_to_start_of_document(self):
        log.stub()

    def move_to_end_of_document(self):
        log.stub()

    def toggle_selection(self):
        log.stub()

    def drop_selection(self):
        log.stub()

    def has_selection(self):
        return self._widget.hasSelection()

    def selection(self, html=False):
        if html:
            raise browsertab.UnsupportedOperationError
        return self._widget.selectedText()

    def _follow_selected_cb(self, js_elem, tab=False):
        """Callback for javascript which clicks the selected element.

        Args:
            js_elem: The element serialized from javascript.
            tab: Open in a new tab.
        """
        if js_elem is None:
            return
        assert isinstance(js_elem, dict), js_elem
        elem = webengineelem.WebEngineElement(js_elem, tab=self._tab)
        if tab:
            click_type = usertypes.ClickTarget.tab
        else:
            click_type = usertypes.ClickTarget.normal

        # Only click if we see a link
        if elem.is_link():
            log.webview.debug("Found link in selection, clicking. ClickTarget "
                              "{}, elem {}".format(click_type, elem))
            elem.click(click_type)

    def follow_selected(self, *, tab=False):
        if self._tab.search.search_displayed:
            # We are currently in search mode.
            # let's click the link via a fake-click
            # https://bugreports.qt.io/browse/QTBUG-60673
            self._tab.search.clear()

            log.webview.debug("Clicking a searched link via fake key press.")
            # send a fake enter, clicking the orange selection box
            if tab:
                self._tab.key_press(Qt.Key_Enter, modifier=Qt.ControlModifier)
            else:
                self._tab.key_press(Qt.Key_Enter)

        else:
            # click an existing blue selection
            js_code = javascript.assemble('webelem', 'find_selected_link')
            self._tab.run_js_async(js_code, lambda jsret:
                                   self._follow_selected_cb(jsret, tab))


class WebEngineScroller(browsertab.AbstractScroller):

    """QtWebEngine implementations related to scrolling."""

    def __init__(self, tab, parent=None):
        super().__init__(tab, parent)
        self._pos_perc = (0, 0)
        self._pos_px = QPoint()
        self._at_bottom = False

    def _init_widget(self, widget):
        super()._init_widget(widget)
        page = widget.page()
        page.scrollPositionChanged.connect(self._update_pos)

    def _repeated_key_press(self, key, count=1, modifier=Qt.NoModifier):
        """Send count fake key presses to this scroller's WebEngineTab."""
        for _ in range(min(count, 5000)):
            self._tab.key_press(key, modifier)

    @pyqtSlot()
    def _update_pos(self):
        """Update the scroll position attributes when it changed."""
        def update_pos_cb(jsret):
            """Callback after getting scroll position via JS."""
            if jsret is None:
                # This can happen when the callback would get called after
                # shutting down a tab
                return
            log.webview.vdebug(jsret)
            assert isinstance(jsret, dict), jsret
            self._pos_px = QPoint(jsret['px']['x'], jsret['px']['y'])

            dx = jsret['scroll']['width'] - jsret['inner']['width']
            if dx == 0:
                perc_x = 0
            else:
                perc_x = min(100, round(100 / dx * jsret['px']['x']))

            dy = jsret['scroll']['height'] - jsret['inner']['height']
            if dy == 0:
                perc_y = 0
            else:
                perc_y = min(100, round(100 / dy * jsret['px']['y']))

            self._at_bottom = dy >= jsret['px']['y']
            self._pos_perc = perc_x, perc_y

            self.perc_changed.emit(*self._pos_perc)

        js_code = javascript.assemble('scroll', 'pos')
        self._tab.run_js_async(js_code, update_pos_cb)

    def pos_px(self):
        return self._pos_px

    def pos_perc(self):
        return self._pos_perc

    def to_perc(self, x=None, y=None):
        js_code = javascript.assemble('scroll', 'to_perc', x, y)
        self._tab.run_js_async(js_code)

    def to_point(self, point):
        js_code = javascript.assemble('window', 'scroll', point.x(), point.y())
        self._tab.run_js_async(js_code)

    def delta(self, x=0, y=0):
        self._tab.run_js_async(javascript.assemble('window', 'scrollBy', x, y))

    def delta_page(self, x=0, y=0):
        js_code = javascript.assemble('scroll', 'delta_page', x, y)
        self._tab.run_js_async(js_code)

    def up(self, count=1):
        self._repeated_key_press(Qt.Key_Up, count)

    def down(self, count=1):
        self._repeated_key_press(Qt.Key_Down, count)

    def left(self, count=1):
        self._repeated_key_press(Qt.Key_Left, count)

    def right(self, count=1):
        self._repeated_key_press(Qt.Key_Right, count)

    def top(self):
        self._tab.key_press(Qt.Key_Home)

    def bottom(self):
        self._tab.key_press(Qt.Key_End)

    def page_up(self, count=1):
        self._repeated_key_press(Qt.Key_PageUp, count)

    def page_down(self, count=1):
        self._repeated_key_press(Qt.Key_PageDown, count)

    def at_top(self):
        return self.pos_px().y() == 0

    def at_bottom(self):
        return self._at_bottom


class WebEngineHistory(browsertab.AbstractHistory):

    """QtWebEngine implementations related to page history."""

    def current_idx(self):
        return self._history.currentItemIndex()

    def back(self):
        self._history.back()

    def forward(self):
        self._history.forward()

    def can_go_back(self):
        return self._history.canGoBack()

    def can_go_forward(self):
        return self._history.canGoForward()

    def serialize(self):
        if not qtutils.version_check('5.9'):
            # WORKAROUND for
            # https://github.com/qutebrowser/qutebrowser/issues/2289
            # Don't use the history's currentItem here, because of
            # https://bugreports.qt.io/browse/QTBUG-59599 and because it doesn't
            # contain view-source.
            scheme = self._tab.url().scheme()
            if scheme in ['view-source', 'chrome']:
                raise browsertab.WebTabError("Can't serialize special URL!")
        return qtutils.serialize(self._history)

    def deserialize(self, data):
        return qtutils.deserialize(data, self._history)

    def load_items(self, items):
        stream, _data, cur_data = tabhistory.serialize(items)
        qtutils.deserialize_stream(stream, self._history)
        if cur_data is not None:
            if 'zoom' in cur_data:
                self._tab.zoom.set_factor(cur_data['zoom'])
            if ('scroll-pos' in cur_data and
                    self._tab.scroller.pos_px() == QPoint(0, 0)):
                QTimer.singleShot(0, functools.partial(
                    self._tab.scroller.to_point, cur_data['scroll-pos']))


class WebEngineZoom(browsertab.AbstractZoom):

    """QtWebEngine implementations related to zooming."""

    def _set_factor_internal(self, factor):
        self._widget.setZoomFactor(factor)

    def factor(self):
        return self._widget.zoomFactor()


class WebEngineElements(browsertab.AbstractElements):

    """QtWebEngine implemementations related to elements on the page."""

    def _js_cb_multiple(self, callback, js_elems):
        """Handle found elements coming from JS and call the real callback.

        Args:
            callback: The callback to call with the found elements.
                      Called with None if there was an error.
            js_elems: The elements serialized from javascript.
        """
        if js_elems is None:
            callback(None)
            return

        elems = []
        for js_elem in js_elems:
            elem = webengineelem.WebEngineElement(js_elem, tab=self._tab)
            elems.append(elem)
        callback(elems)

    def _js_cb_single(self, callback, js_elem):
        """Handle a found focus elem coming from JS and call the real callback.

        Args:
            callback: The callback to call with the found element.
                      Called with a WebEngineElement or None.
            js_elem: The element serialized from javascript.
        """
        debug_str = ('None' if js_elem is None
                     else utils.elide(repr(js_elem), 1000))
        log.webview.debug("Got element from JS: {}".format(debug_str))

        if js_elem is None:
            callback(None)
        else:
            elem = webengineelem.WebEngineElement(js_elem, tab=self._tab)
            callback(elem)

    def find_css(self, selector, callback, *, only_visible=False):
        js_code = javascript.assemble('webelem', 'find_css', selector,
                                      only_visible)
        js_cb = functools.partial(self._js_cb_multiple, callback)
        self._tab.run_js_async(js_code, js_cb)

    def find_id(self, elem_id, callback):
        js_code = javascript.assemble('webelem', 'find_id', elem_id)
        js_cb = functools.partial(self._js_cb_single, callback)
        self._tab.run_js_async(js_code, js_cb)

    def find_focused(self, callback):
        js_code = javascript.assemble('webelem', 'find_focused')
        js_cb = functools.partial(self._js_cb_single, callback)
        self._tab.run_js_async(js_code, js_cb)

    def find_at_pos(self, pos, callback):
        assert pos.x() >= 0
        assert pos.y() >= 0
        pos /= self._tab.zoom.factor()
        js_code = javascript.assemble('webelem', 'find_at_pos',
                                      pos.x(), pos.y())
        js_cb = functools.partial(self._js_cb_single, callback)
        self._tab.run_js_async(js_code, js_cb)


class WebEngineTab(browsertab.AbstractTab):

    """A QtWebEngine tab in the browser."""

    def __init__(self, *, win_id, mode_manager, private, parent=None):
        super().__init__(win_id=win_id, mode_manager=mode_manager,
                         private=private, parent=parent)
        widget = webview.WebEngineView(tabdata=self.data, win_id=win_id,
                                       private=private)
        self.history = WebEngineHistory(self)
        self.scroller = WebEngineScroller(self, parent=self)
        self.caret = WebEngineCaret(win_id=win_id, mode_manager=mode_manager,
                                    tab=self, parent=self)
        self.zoom = WebEngineZoom(win_id=win_id, parent=self)
        self.search = WebEngineSearch(parent=self)
        self.printing = WebEnginePrinting()
        self.elements = WebEngineElements(self)
        self.action = WebEngineAction()
        self._set_widget(widget)
        self._connect_signals()
        self.backend = usertypes.Backend.QtWebEngine
        self._init_js()
        self._child_event_filter = None
        self._saved_zoom = None

    def _init_js(self):
        js_code = '\n'.join([
            '"use strict";',
            'window._qutebrowser = {};',
            utils.read_file('javascript/scroll.js'),
            utils.read_file('javascript/webelem.js'),
        ])
        script = QWebEngineScript()
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setSourceCode(js_code)

        page = self._widget.page()
        script.setWorldId(QWebEngineScript.ApplicationWorld)

        # FIXME:qtwebengine  what about runsOnSubFrames?
        page.scripts().insert(script)

    def _install_event_filter(self):
        self._widget.focusProxy().installEventFilter(self._mouse_event_filter)
        self._child_event_filter = mouse.ChildEventFilter(
            eventfilter=self._mouse_event_filter, widget=self._widget,
            parent=self)
        self._widget.installEventFilter(self._child_event_filter)

    @pyqtSlot()
    def _restore_zoom(self):
        if self._saved_zoom is None:
            return
        self.zoom.set_factor(self._saved_zoom)
        self._saved_zoom = None

    def openurl(self, url):
        self._saved_zoom = self.zoom.factor()
        self._openurl_prepare(url)
        self._widget.load(url)

    def url(self, requested=False):
        page = self._widget.page()
        if requested:
            return page.requestedUrl()
        else:
            return page.url()

    def dump_async(self, callback, *, plain=False):
        if plain:
            self._widget.page().toPlainText(callback)
        else:
            self._widget.page().toHtml(callback)

    def run_js_async(self, code, callback=None, *, world=None):
        if world is None:
            world_id = QWebEngineScript.ApplicationWorld
        elif isinstance(world, int):
            world_id = world
        else:
            world_id = _JS_WORLD_MAP[world]

        if callback is None:
            self._widget.page().runJavaScript(code, world_id)
        else:
            self._widget.page().runJavaScript(code, world_id, callback)

    def shutdown(self):
        self.shutting_down.emit()
        if qtutils.version_check('5.8', exact=True):
            # WORKAROUND for
            # https://bugreports.qt.io/browse/QTBUG-58563
            self.search.clear()
        self._widget.shutdown()

    def reload(self, *, force=False):
        if force:
            action = QWebEnginePage.ReloadAndBypassCache
        else:
            action = QWebEnginePage.Reload
        self._widget.triggerPageAction(action)

    def stop(self):
        self._widget.stop()

    def title(self):
        return self._widget.title()

    def icon(self):
        return self._widget.icon()

    def set_html(self, html, base_url=QUrl()):
        # FIXME:qtwebengine
        # check this and raise an exception if too big:
        # Warning: The content will be percent encoded before being sent to the
        # renderer via IPC. This may increase its size. The maximum size of the
        # percent encoded content is 2 megabytes minus 30 bytes.
        self._widget.setHtml(html, base_url)

    def networkaccessmanager(self):
        return None

    def user_agent(self):
        return None

    def clear_ssl_errors(self):
        raise browsertab.UnsupportedOperationError

    def key_press(self, key, modifier=Qt.NoModifier):
        press_evt = QKeyEvent(QEvent.KeyPress, key, modifier, 0, 0, 0)
        release_evt = QKeyEvent(QEvent.KeyRelease, key, modifier,
                                0, 0, 0)
        self.send_event(press_evt)
        self.send_event(release_evt)

    @pyqtSlot()
    def _on_history_trigger(self):
        url = self.url()
        requested_url = self.url(requested=True)

        # Don't save the title if it's generated from the URL
        title = self.title()
        title_url = QUrl(url)
        title_url.setScheme('')
        if title == title_url.toDisplayString(QUrl.RemoveScheme).strip('/'):
            title = ""

        # Don't add history entry if the URL is invalid anyways
        if not url.isValid():
            log.misc.debug("Ignoring invalid URL being added to history")
            return

        self.add_history_item.emit(url, requested_url, title)

    @pyqtSlot(QUrl, 'QAuthenticator*')
    def _on_authentication_required(self, url, authenticator):
        # FIXME:qtwebengine support .netrc
        answer = shared.authentication_required(
            url, authenticator, abort_on=[self.shutting_down,
                                          self.load_started])
        if answer is None:
            try:
                # pylint: disable=no-member, useless-suppression
                sip.assign(authenticator, QAuthenticator())
            except AttributeError:
                # WORKAROUND for
                # https://www.riverbankcomputing.com/pipermail/pyqt/2016-December/038400.html
                url_string = url.toDisplayString()
                error_page = jinja.render(
                    'error.html',
                    title="Error loading page: {}".format(url_string),
                    url=url_string, error="Authentication required", icon='')
                self.set_html(error_page)

    @pyqtSlot('QWebEngineFullScreenRequest')
    def _on_fullscreen_requested(self, request):
        request.accept()
        on = request.toggleOn()

        self.data.fullscreen = on
        self.fullscreen_requested.emit(on)
        if on:
            notification = miscwidgets.FullscreenNotification(self)
            notification.show()
            notification.set_timeout(3000)

    @pyqtSlot()
    def _on_load_started(self):
        """Clear search when a new load is started if needed."""
        if qtutils.version_check('5.9'):
            # WORKAROUND for
            # https://bugreports.qt.io/browse/QTBUG-61506
            self.search.clear()
        super()._on_load_started()

    @pyqtSlot(QWebEnginePage.RenderProcessTerminationStatus, int)
    def _on_render_process_terminated(self, status, exitcode):
        """Show an error when the renderer process terminated."""
        if (status == QWebEnginePage.AbnormalTerminationStatus and
                exitcode == 256):
            # WORKAROUND for https://bugreports.qt.io/browse/QTBUG-58697
            status = QWebEnginePage.CrashedTerminationStatus

        status_map = {
            QWebEnginePage.NormalTerminationStatus:
                browsertab.TerminationStatus.normal,
            QWebEnginePage.AbnormalTerminationStatus:
                browsertab.TerminationStatus.abnormal,
            QWebEnginePage.CrashedTerminationStatus:
                browsertab.TerminationStatus.crashed,
            QWebEnginePage.KilledTerminationStatus:
                browsertab.TerminationStatus.killed,
            -1:
                browsertab.TerminationStatus.unknown,
        }
        self.renderer_process_terminated.emit(status_map[status], exitcode)

    def _connect_signals(self):
        view = self._widget
        page = view.page()

        page.windowCloseRequested.connect(self.window_close_requested)
        page.linkHovered.connect(self.link_hovered)
        page.loadProgress.connect(self._on_load_progress)
        page.loadStarted.connect(self._on_load_started)
        page.loadFinished.connect(self._on_history_trigger)
        page.loadFinished.connect(self._restore_zoom)
        page.loadFinished.connect(self._on_load_finished)
        page.certificate_error.connect(self._on_ssl_errors)
        page.authenticationRequired.connect(self._on_authentication_required)
        page.fullScreenRequested.connect(self._on_fullscreen_requested)
        page.contentsSizeChanged.connect(self.contents_size_changed)

        view.titleChanged.connect(self.title_changed)
        view.urlChanged.connect(self._on_url_changed)
        view.renderProcessTerminated.connect(
            self._on_render_process_terminated)
        view.iconChanged.connect(self.icon_changed)

    def event_target(self):
        return self._widget.focusProxy()
