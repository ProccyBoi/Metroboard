# Metroboard Assets

Optional board artwork for the web simulator lives here.

Place a straight-on, cropped image of the physical board at one of these paths:

- `assets/board_overlay.png`
- `assets/board_overlay.jpg`
- `assets/board_overlay.jpeg`
- `assets/board_overlay.webp`

The simulator stretches the image to the `/board_layout` viewBox, currently
`1120 x 1040`, then draws the interactive station hit targets on top. For the
best result, crop the image to the board edges and remove perspective distortion
before deploying it.

You can also set `BOARD_OVERLAY_PATH` for another local file or
`BOARD_OVERLAY_URL` for an externally hosted image.
