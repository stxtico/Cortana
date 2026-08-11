# L-bracket

A right-angle mounting bracket: a horizontal base flange and a vertical
upright flange, joined along one shared edge with a rounded inner fillet.
Each flange carries one mounting hole, centered across the part's depth.

## Mounts to

Two flat surfaces at 90 degrees to each other - a shelf mount, a motor
stand-off, or joining two perpendicular panels. One through-hole per
flange, sized for a standard machine screw (`hole_dia`).

## Constraints

- `thickness` must stay comfortably above 0 and well under `hole_inset` -
  this is a thin, flat bracket, not a solid block.
- `fillet_radius` must stay smaller than `thickness`, or the inner corner
  fillet can't be cut cleanly.
- `hole_inset` on each flange is measured from that flange's far edge, and
  must leave enough material around the hole to stay printable (roughly
  `hole_inset > hole_dia`).
- Holes are drilled straight through their flange, centered across `depth`.
