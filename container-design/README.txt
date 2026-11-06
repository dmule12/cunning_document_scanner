ICE-SLEEVE CONTAINER WITH BOTTLE HOLD-DOWN LID
==============================================

Revision 3, for the LENO 1200 ml / 42 oz single-wall stainless wide-mouth
bottle (Temu size chart: W 3.6 in = 91 mm, H 8.6 in = 218 mm). Replaces
revision 2, which was for a PET soda bottle and does not fit this one.

The body is the same 127 mm x 180 mm cylinder as before plus three small
bayonet lugs on the outside near the rim. The lid is new.

HOW IT HOLDS THE BOTTLE DOWN
----------------------------
The bottle has no neck ring, so the lid catches its SHOULDER instead.
A twist-lock ring sits on the body rim; a short tube rises from it around
the bottle; on top is a plate with a 70 mm hole. The bottle's threads
pass through the hole, its shoulder taper does not. When the bottle tries
to float it rises about 10 mm, the shoulder meets the chamfered edge of
the hole, and the lid, locked to the body by the lugs, stops it.

The plate has three openings so you can top up water after locking and
so the ice has somewhere to expand.

WHY THE DEFAULTS MIGHT BE WRONG (MEASURE BEFORE PRINTING THE LID)
----------------------------------------------------------------
The size chart does not say whether 218 mm includes the cap, and gives no
neck or shoulder size. The .scad assumes 218 mm is WITHOUT the cap, a
58 mm thread diameter and a 25 mm tall shoulder. Stand the bottle on a
table and measure these four things, then edit the matching parameters:

  bottle_body_diameter    widest diameter of the straight body        (91.4)
  bottle_neck_diameter    outside diameter across the threads         (58)
  bottle_body_height      table to where the straight side ends       (173)
  bottle_shoulder_height  from there up to the bottom of the neck     (25)

Which way errors are safe: if the real shoulder is LOWER than modelled,
the bottle just floats up a few more millimetres before it stops. If it
is HIGHER, the lid cannot seat on the rim and the lugs will not engage.
So when unsure, round the heights up or raise lift_margin (default 10).
If the shoulder turns out to be below the rim, the .scad automatically
drops the tube and makes a flat lid instead.

hole_diameter (70) must be bigger than the threads and smaller than the
body. If you want to lock the lid with the cap on, it must also be bigger
than the cap; a bigger hole just moves the contact lower on the shoulder
and the .scad recomputes the height.

With the defaults the plate sits 22 mm above the rim and the bottle
floats up 10 mm before the shoulder stops against it.

USING IT
--------
1. Bottle into the body, empty and capped (or with its cap off).
2. Fill the body about 3/4 with water.
3. Lower the lid over the bottle with the three index grooves on the
   skirt over the three raised ribs on the body, so the notches drop
   over the lugs.
4. Press the lid down onto the rim and twist CLOCKWISE (seen from above)
   about 25 degrees until it stops.
5. Top up water through the plate openings. Do not fill to the rim.
6. To release: twist counter-clockwise and lift.

No-print alternative: the empty bottle has roughly 0.9 kg of net lift in
this body, so about 1 kg of anything dense inside it (rice, coins, bolts)
also keeps it down. Water inside works too, but it freezes with the rest.

PRINTING
--------
container-body-127x180mm.stl  base on the bed, opening up. The lugs
                              overhang 1.6 mm; no supports needed.
container-lid-chimney.stl     already flipped: flat plate on the bed,
                              skirt at the top. No supports needed; the
                              only overhangs are 45 degree flares and
                              2 mm bridges inside the bayonet grooves.

Clearances are 0.4 mm radial (skirt to body, lug to slot). If the lid is
too tight raise fit_clearance to 0.5; if sloppy, lower it to 0.3. PETG is
a better choice than PLA for parts that live in a freezer with ice.

THINGS TO BE AWARE OF
---------------------
Ice expands about 9 % and freezes from the outside in; a rigid printed
cylinder full of water can crack. The open plate and the empty bottle in
the middle both help, but nothing here has been freeze-tested. Do not
fill to the rim. Running warm water over the outside of the body releases
the ice block afterwards.

The body dimensions are still the unverified estimates from revision 1.

FILES
-----
container-draft.scad            editable parametric OpenSCAD source
container-body-127x180mm.stl    body with lugs (unchanged since rev 2)
container-lid-chimney.stl       lid, print orientation
preview-*.png                   renders: assembly with bottle, cutaway, lid

Both STLs were checked as single closed watertight meshes with consistent
outward normals. CSG checks of the locked assembly: no interference
between lid, body and bottle; the lid lifts off freely when unlocked;
the lugs bear on the slot floors after 0.4 mm of lift; the bottle's
shoulder meets the plate hole after 10 mm of lift. Digital checks only;
no physical print or freeze was tested.

Regenerate from the .scad:
  openscad -D 'part="body"' -o container-body-127x180mm.stl container-draft.scad
  openscad -D 'part="lid"'  -o container-lid-chimney.stl    container-draft.scad
