# From Hallmark

Taken from [Hallmark](https://github.com/Nutlope/hallmark) (MIT, see `LICENSE`),
commit `7ec868225c6caa040558c321ee0fefa0e73f83a8`, on 2026-10-26, for Paul.

- `themes/` - 19 of its 48 themes, the ones that suit a local business and
  load their faces from Google Fonts (Atelier and Kiln use Fontshare and were
  left out). Each is a material - paper, ink, type, surfaces - with a picture
  of a finished page. Untouched; where a theme names Hallmark's own files
  (the stamp, `texture.md`, `layout.md`) Paul has none of them and goes by
  his own rules. `paul.py brief` deals three when the owner gave no reference.
- `photos.json` - its list of openly licensed Wikimedia Commons photographs
  (CC0, public domain, CC BY, CC BY-SA), less the tech and science ones. It
  holds no images, only where each is and whose it is. `paul.py stock`
  searches it and Commons, downloads at the size a page needs, and records
  the credit that code writes onto every page.

The rules Paul took from Hallmark are not copied here: they are rewritten
into `scripts/paul.py` as checks, and into his instructions, so there is one
rulebook.
