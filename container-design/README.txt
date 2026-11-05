ICE-SLEEVE CONTAINER WITH BOTTLE HOLD-DOWN LID
==============================================

Revision 2. Adds a twist-lock lid that keeps a water bottle submerged in
the water-filled body so the water can freeze around it. The body is the
same 127 mm x 180 mm straight-sided cylinder as the first draft, plus
three small bayonet lugs on the outside near the rim.

HOW IT HOLDS THE BOTTLE DOWN
----------------------------
Every standard PET bottle has a "support ring": the flange just under the
cap threads and tamper band. The lid has a 31.5 mm hole in its centre hub.
The threads and tamper band pass through the hole; the 33 mm support ring
does not. So the bottle hangs from its neck.

The lid itself is a spider (hub, three spokes, outer ring) with a 16 mm
skirt that fits over the outside of the body. The skirt has three L-shaped
bayonet slots. Three lugs on the body ride up the slots and lock when the
lid is twisted. Buoyancy pushes the bottle up into the hub, the hub pulls
the lid up, and the lugs stop the lid. Nothing has to be clamped or tied.

The spider is open on purpose: you can top up the water through the
openings after locking, and the ice has a free surface to expand into.

USING IT
--------
1. Put the lid over the bottle neck (lid flat side up, skirt down), and
   screw the bottle cap back on above the lid if the bottle is open.
2. Fill the body about 3/4 with water.
3. Lower the lid + bottle onto the body with the three index grooves on
   the skirt lined up with the three raised ribs on the body. The skirt
   notches then drop over the lugs.
4. Press the lid down until its ring sits on the rim, then twist it
   CLOCKWISE (seen from above) about 25 degrees until it stops.
5. Top up the water through the spider openings and freeze.
6. To release: twist counter-clockwise, lift the lid off the neck.

If you want the bottle to hang higher in the body (more ice under it),
slip the 10 mm spacer ring over the neck between the support ring and the
lid hub before step 1.

CHECK YOUR BOTTLE
-----------------
Everything above assumes a standard 28 mm soda/water bottle neck
(PCO 1881: thread + tamper band about 30 mm across, support ring 33 mm).
Measure yours. neck_hole_diameter in the .scad must be bigger than the
threads and tamper band and smaller than the support ring. Printed holes
tend to come out 0.2 to 0.4 mm undersize; if the band will not pass, open
the hole with a drill or raise the parameter by 0.5 mm.

The body is 177 mm deep inside. The bottle's height from its base to the
underside of its support ring must be less than that or the bottle will
stand on the base before the lid can seat; a typical 500 mL bottle is
right at this limit, a 330 mL or 400 mL bottle hangs with room below.
Raise total_height in the .scad if you need a taller body.

PRINTING
--------
container-body-127x180mm.stl   base on the bed, opening up. The lugs
                               overhang 1.6 mm, which prints without
                               supports. 3+ perimeters, 100 % infill not
                               needed; the walls are solid anyway.
container-lid-bayonet.stl      already flipped: flat face on the bed,
                               skirt pointing up. No supports needed; the
                               groove roofs inside the skirt are 2 mm
                               bridges.
neck-spacer-10mm.stl           optional, prints flat.

Clearances are 0.4 mm radial (skirt to body, lug to slot). If the lid is
too tight on your printer, raise fit_clearance to 0.5; if sloppy, lower it
to 0.3. PETG is a better choice than PLA for the body if it is going to
live in a freezer with ice in it.

THINGS TO BE AWARE OF
---------------------
Ice expands about 9 % when it freezes and freezes from the outside in.
A rigid printed cylinder full of water can crack. The open lid helps
(ice can push up through the openings) and an empty, capped bottle in the
middle acts as a crush zone, but this draft has not been freeze-tested.
Do not fill right to the rim. If the body cracks, thicker walls or a
slightly tapered body are the usual fixes.

Freezing the bottle into the ice also means the bottle is locked in the
block afterwards. Running warm water over the outside of the body releases
the block from the body.

The body dimensions are still the unverified estimates from revision 1
(carafe body diameter inferred from a listing, height scaled from a photo).

FILES
-----
container-draft.scad            editable parametric OpenSCAD source
container-body-127x180mm.stl    body with lugs
container-lid-bayonet.stl       lid, print orientation
neck-spacer-10mm.stl            optional spacer ring
preview-*.png                   renders: assembly, cutaway, lid, body

All three STLs were checked as single closed watertight meshes with
consistent outward normals and correct bounding boxes. A CSG check of
the locked assembly shows no interference between lid and body, the lid
lifts off freely when unlocked, and the lugs bear on the slot floors
after 0.4 mm of lift when locked. These are digital checks only; no
physical print or freeze was tested.

Regenerate from the .scad:
  openscad -D 'part="body"'   -o container-body-127x180mm.stl container-draft.scad
  openscad -D 'part="lid"'    -o container-lid-bayonet.stl     container-draft.scad
  openscad -D 'part="spacer"' -o neck-spacer-10mm.stl          container-draft.scad
