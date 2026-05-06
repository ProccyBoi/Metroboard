# Metroboard Assets

Optional board artwork for the web simulator lives here.

Place a straight-on, cropped image of the physical board at one of these paths:

- `assets/board_overlay.png`
- `assets/board_overlay.jpg`
- `assets/board_overlay.jpeg`
- `assets/board_overlay.webp`

The included `board_overlay.png` was sourced from the Transport NSW Sydney
Trains network map page:

https://transportnsw.info/sydney-trains-network-map

The simulator uses this artwork's pixel space as the `/board_layout` viewBox,
currently `861 x 850`, then draws the interactive station hit targets on top.
For the best result with a replacement image, crop it to the same map bounds and
dimensions.

You can also set `BOARD_OVERLAY_PATH` for another local file or
`BOARD_OVERLAY_URL` for an externally hosted image.
