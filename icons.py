"""The site's own mark (LOGO: a page with the AI sparkle on it and a pen) and small line icons for the UI's section headings, drawn inline so nothing loads from elsewhere. Each is a
24x24 outline that takes the text color (styled by .ico in ui.UI_CSS and render's CSS)."""
from urllib.parse import quote

PATHS = {
    # anything waiting on a human decision: a finger tapping
    "touch": '<path d="M9 14V5.5a1.5 1.5 0 0 1 3 0V11"/><path d="M12 10.5V9a1.5 1.5 0 0 1 3 0v2"/>'
             '<path d="M15 11a1.5 1.5 0 0 1 3 0v4a6 6 0 0 1-6 6h-1a6 6 0 0 1-4.9-2.5l-2.4-3.4a1.5 1.5 0 0 1 2.4-1.8L9 15"/>'
             '<path d="M5.5 5.5a6 6 0 0 1 10 0"/>',
    "brain": '<path d="M12 5a3 3 0 0 0-5.6-1.5A3 3 0 0 0 4 8a3 3 0 0 0 0 5 3 3 0 0 0 2.5 4.5A3 3 0 0 0 12 19z"/>'
             '<path d="M12 5a3 3 0 0 1 5.6-1.5A3 3 0 0 1 20 8a3 3 0 0 1 0 5 3 3 0 0 1-2.5 4.5A3 3 0 0 1 12 19z"/>'
             '<path d="M8 9.5c1 .3 1.8 1 2 2M16 9.5c-1 .3-1.8 1-2 2"/>',
    "pencil": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    # the cork board: a card with two pins and the string between them
    "board": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.3"/>'
             '<circle cx="16" cy="14" r="1.3"/><path d="m8 9 8 5"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/>'
            '<circle cx="4.5" cy="18" r="1"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "write": '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h6"/>',
    "send": '<path d="M21 3 3 10.5l7 2.5 2.5 7z"/><path d="M21 3 10 13"/>',
    "newspaper": '<path d="M4 5h13v14H6a2 2 0 0 1-2-2z"/><path d="M17 9h3v8a2 2 0 0 1-2 2"/>'
                 '<path d="M7 9h7M7 13h7M7 16h4"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M12 15h5"/>',
    "alert": '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
    "check": '<path d="m5 12 4.5 4.5L19 7"/>',
    "bulb": '<path d="M9 18h6M10 21h4"/>'
            '<path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>',
    "scale": '<path d="M12 4v16M7 20h10M5 7h14"/><path d="m5 7-3 6a3 3 0 0 0 6 0zM19 7l-3 6a3 3 0 0 0 6 0z"/>',
    "person": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/>',
    "people": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/>'
              '<path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2.2.6 3.5 2.8 3.5 6"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    "message": '<path d="M4 5h16v11H9l-5 4z"/>',
    "cross": '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
    "plug": '<path d="M9 2v5M15 2v5"/><path d="M6 7h12v4a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
    "monitor": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
             '<path d="M12 3c3 3 3.6 6 3.6 9s-.6 6-3.6 9c-3-3-3.6-6-3.6-9s.6-6 3.6-9z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19'
           'M19 5l-1.5 1.5M6.5 17.5 5 19"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    "palette": '<path d="M12 3a9 9 0 0 0 0 18c1.1 0 1.8-.8 1.8-1.8 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 '
               '0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4c0-4.4-4-8-9-8z"/><circle cx="7.5" cy="11" r="1"/>'
               '<circle cx="10.5" cy="7" r="1"/><circle cx="15" cy="7.5" r="1"/>',
    "rss": '<path d="M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14"/><circle cx="6" cy="18" r="1.5"/>',
    # the pipeline strip's tiles (ui.flow_strip)
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "compass": '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
    "book": '<path d="M4 5a2 2 0 0 1 2-2h13v15H6a2 2 0 0 0-2 2z"/><path d="M4 20a2 2 0 0 0 2 2h13v-4"/>',
    "shield": '<path d="M12 3 4.5 6v5.5c0 4.5 3.2 8 7.5 9.5 4.3-1.5 7.5-5 7.5-9.5V6z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
    "play": '<path d="M7 4.5v15l12.5-7.5z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "broadcast": '<circle cx="12" cy="12" r="2"/><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7'
                 'M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8"/>',
}

def icon(name: str) -> str:
    """An inline icon, hidden from screen readers (the heading beside it says what it is)."""
    return f'<span class="ico" aria-hidden="true"><svg viewBox="0 0 24 24">{PATHS[name]}</svg></span>'

CSS = """.ico { display:inline-flex; flex:none; width:1.05em; height:1.05em; }
.ico svg { width:100%; height:100%; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round;
           stroke-linejoin:round; }
"""

# The mark: a sheet of paper with the four-pointed AI sparkle on it and a pen. Its own colors, so it reads the same in
# light and dark. Used as the favicon (FAVICON, in the page head) and beside the name (logo()), in the app and on the
# published site, local or GitHub Pages. Drawn inline, like the icons: nothing loads from elsewhere.
LOGO = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<path d="M7 3h13l6 6v20a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" fill="#fff" stroke="#1f2937" stroke-width="1.8" stroke-linejoin="round"/>'
        '<path d="M20 3v6h6" fill="#e5e7eb" stroke="#1f2937" stroke-width="1.8" stroke-linejoin="round"/>'
        '<path d="M13 10Q14 16 20 17Q14 18 13 24Q12 18 6.5 17Q12 16 13 10z" fill="#7c3aed"/>'
        '<g transform="translate(24 18) rotate(45)">'
        '<rect x="-2.3" y="-9" width="4.6" height="11" rx="1" fill="#f59e0b" stroke="#1f2937" stroke-width="1.4"/>'
        '<path d="M-2.3 2L0 6.5L2.3 2z" fill="#fde68a" stroke="#1f2937" stroke-width="1.4" stroke-linejoin="round"/>'
        '</g>'
        '</svg>')
FAVICON = f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{quote(LOGO)}">'

def logo() -> str:
    """The mark at the size of the text beside it, hidden from screen readers (the name next to it says it)."""
    return LOGO.replace("<svg ", '<svg class="logo" aria-hidden="true" width="1.3em" height="1.3em" '
                        'style="vertical-align:-.28em;margin-right:.4em;flex:none" ', 1)
