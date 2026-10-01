---
name: file-handling
description: Read images and large files without destroying the conversation. Use before reading any image — a screenshot you took, a chart you rendered, a photo the user sent — and before reading a file you have not sized. Reading an oversized image ends the conversation permanently and cannot be undone.
---

# File handling

What you read becomes permanent. Every `read` is inlined into this conversation
and replayed to the model on every later turn — you cannot remove it, edit it, or
apologise for it. Two kinds of read are unrecoverable, and both are avoidable in
one extra command.

## Images: longest edge under 2000px, always

**The failure.** The API caps how large an image may be. That cap is *not fixed*:
once a conversation carries more than 20 images it tightens to **2000px on the
longest edge**, and it is applied to the whole conversation — so an image that was
legal when you read it becomes illegal later, when some unrelated 21st image
pushes the count over. The request is then rejected outright, every following turn
replays the same illegal image, and **the conversation is dead for good.** Not
slow, not degraded — dead, with no way to remove the offending block.

**The rule: never read an image whose longest edge is 2000px or more.** Under that
size nothing can retroactively invalidate it, whatever else the conversation
accumulates.

Check before you read, whenever you did not create the file at a known size:

```bash
python3 -c "from PIL import Image; print(Image.open('shot.png').size)"
```

Downscale in place if it is over — 1600px is a safe target that stays sharp:

```bash
python3 -c "from PIL import Image; p='shot.png'; im=Image.open(p); im.thumbnail((1600,1600)); im.save(p)"
```

### Screenshots are the usual culprit

Two habits produce oversized files without looking like they do:

| Habit | What you get | Fix |
|---|---|---|
| `device_scale_factor=2` | doubles both edges — a 1800×1400 viewport becomes 3600×2800 | use `device_scale_factor=1` |
| `full_page=True` on a long page | width fine, height enormous — e.g. 980×4262 | capture viewport sections and read them one at a time |

Note the second one: a full-page capture is usually *within* the limit on width
and far over it on height. Checking only the width will not save you.

```python
# safe defaults for a page screenshot
page = browser.new_page(viewport={"width": 1440, "height": 900})  # scale factor 1
page.screenshot(path="shot.png")                                   # not full_page
```

### Keep the count down too

The cap only tightens past 20 images, so fewer images is its own protection — and
each one costs tokens on every subsequent turn.

- Re-read **one** file as you revise it, rather than saving `v1.png`, `v2.png`,
  `v3.png` and reading each.
- Crop to the region you are actually checking instead of reading the whole frame.
- When you have inspected an image and acted on it, do not read it again to
  confirm — you still have your own description of it.

## Large text files: look before you read

A whole-file `read` of something large is not fatal, but it is irreversible and it
crowds out everything else for the rest of the conversation. Size it first:

```bash
wc -lc path/to/file
```

For anything big, retrieve only what you need — `grep` for the symbol, `head`/
`tail` for structure, or `read` with a line range. Reach for a whole-file read
when the file is genuinely small or you genuinely need all of it.

## Files you produce for the user

Write deliverables to `/mnt/session/outputs/` — files there are captured and can
be handed back to the user. Do not read a file back purely to prove you wrote it;
`ls -la` confirms it exists and costs nothing.
