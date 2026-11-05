// ICE-SLEEVE CONTAINER WITH BOTTLE HOLD-DOWN LID
// ------------------------------------------------------------------
// PROVISIONAL DRAFT: body dimensions are estimates, NOT verified
// measurements (see README). Measure the real vessel before relying on them.
//
// Problem solved by this revision: a bottle placed in the water-filled
// body floats. The lid below hangs the bottle from its own neck ring
// (the "support ring" flange under the cap threads), and the lid itself
// twist-locks onto bayonet lugs on the outside of the body. Buoyancy then
// pushes the bottle up into the lid hub, and the lid up into the lugs, so
// the bottle stays submerged while the water freezes around it.
//
// The lid is an open spider (hub + spokes + outer ring) rather than a
// solid disc, so you can top up water after locking and so the ice has a
// free surface to expand upward into.
//
// Parts:  part = "body" | "lid" | "spacer" | "assembly"
//   body     - the original cylinder plus 3 external bayonet lugs
//   lid      - spider lid, output already flipped for printing (flat side down)
//   spacer   - optional ring that lifts the bottle's hang point by spacer_height
//   assembly - body + lid shown in the LOCKED position (for checking only)
//
// Regenerate STLs (from this directory):
//   openscad -D 'part="body"'   -o container-body-127x180mm.stl container-draft.scad
//   openscad -D 'part="lid"'    -o container-lid-bayonet.stl     container-draft.scad
//   openscad -D 'part="spacer"' -o neck-spacer-10mm.stl          container-draft.scad
//
// To lock: drop the lid on with the skirt notches over the lugs, press it
// down onto the rim, then twist CLOCKWISE (seen from above) by lock_rotation.
// Counter-clockwise releases it.
// ------------------------------------------------------------------

part = "assembly"; // ["body", "lid", "spacer", "assembly"]

// ---- BODY (unchanged from the first draft) -----------------------
outside_diameter = 127;  // ESTIMATED; replace with measured base diameter
total_height     = 180;  // ESTIMATED; replace with measured base-to-red-line height
wall_thickness   = 2.4;  // proposed wall thickness
base_thickness   = 3;    // proposed solid base thickness

// ---- BOTTLE NECK ---------------------------------------------------
// The hole must be LARGER than everything above the support ring (threads,
// tamper-evident band, which is about 30 mm on a standard 28 mm PCO 1881
// soda/water bottle) and SMALLER than the support ring itself (33 mm on
// PCO 1881). Printed holes usually come out 0.2-0.4 mm undersize, so
// measure your bottle and check the first print against it.
neck_hole_diameter = 31.5;
hub_width          = 8;    // radial width of solid material around the hole

// ---- LID -----------------------------------------------------------
lid_thickness      = 4;    // thickness of hub, spokes and outer ring
spoke_count        = 3;
spoke_width        = 14;
ring_inner_overlap = 5;    // how far the outer ring reaches in past the inner wall
skirt_height       = 16;   // how far the skirt drops below the rim
skirt_thickness    = 4;    // must exceed lug_protrusion + fit_clearance + ~2 mm
fit_clearance      = 0.4;  // radial gap: skirt to body, lug to channel

// ---- BAYONET LUGS --------------------------------------------------
lug_count         = 3;
lug_height        = 3;     // vertical size of each lug
lug_protrusion    = 1.6;   // how far each lug sticks out from the body wall
lug_arc_width     = 8;     // circumferential length of each lug, mm
lug_top_below_rim = 7;     // distance from the rim down to the top of the lug
lock_rotation     = 25;    // degrees of twist to lock
index_marks       = true;  // raised rib under each lug + groove on skirt at each notch

// ---- OPTIONAL SPACER -----------------------------------------------
spacer_height = 10;        // lifts the bottle this much higher in the body

$fn = 256;

// ---- derived -------------------------------------------------------
Ro        = outside_diameter / 2;
Ri        = Ro - wall_thickness;
H         = total_height;
skirt_ir  = Ro + fit_clearance;
skirt_or  = skirt_ir + skirt_thickness;
groove_or = skirt_ir + lug_protrusion + fit_clearance;   // radial depth of channel/groove
ring_ir   = Ri - ring_inner_overlap;
hub_or    = neck_hole_diameter / 2 + hub_width;
lug_z1    = H - lug_top_below_rim;                       // lug top
lug_z0    = lug_z1 - lug_height;                         // lug bottom
lug_deg   = lug_arc_width / Ro * 180 / PI;
chan_deg  = (lug_arc_width + 2 * fit_clearance) / Ro * 180 / PI;

assert(outside_diameter > 2 * wall_thickness);
assert(total_height > base_thickness);
assert(wall_thickness > 0 && base_thickness > 0);
assert(skirt_thickness > lug_protrusion + fit_clearance + 1.5, "skirt too thin for groove");
assert(skirt_height > lug_top_below_rim + lug_height + 2, "skirt too short to reach lugs");
assert(ring_ir > hub_or + 10, "spokes too short");
assert(lock_rotation + chan_deg < 360 / lug_count - 5, "grooves would run into each other");

// ---- BODY ----------------------------------------------------------
module lug() {
    rotate([0, 0, -lug_deg / 2])
        rotate_extrude(angle = lug_deg)
            // plain block; the 1.6 mm underside overhang prints fine without support
            translate([Ro - 0.4, lug_z0]) square([0.4 + lug_protrusion, lug_height]);
}

module index_rib() {
    // small raised line on the body just below where the skirt ends, so you
    // can line it up with the groove on the skirt before dropping the lid on
    translate([Ro - 0.4, -0.75, H - skirt_height - 12])
        cube([0.4 + 0.8, 1.5, 10]);
}

module body() {
    difference() {
        cylinder(d = outside_diameter, h = H);
        translate([0, 0, base_thickness])
            cylinder(d = outside_diameter - 2 * wall_thickness, h = H - base_thickness + 1);
    }
    for (i = [0 : lug_count - 1])
        rotate([0, 0, i * 360 / lug_count]) {
            lug();
            if (index_marks) index_rib();
        }
}

// ---- LID (modelled seated on the body, unlocked, in body coordinates) ----
module lid_seated() {
    difference() {
        union() {
            // outer ring resting on the rim
            translate([0, 0, H]) difference() {
                cylinder(r = skirt_or, h = lid_thickness);
                translate([0, 0, -1]) cylinder(r = ring_ir, h = lid_thickness + 2);
            }
            // hub
            translate([0, 0, H]) cylinder(r = hub_or, h = lid_thickness);
            // spokes
            for (i = [0 : spoke_count - 1])
                rotate([0, 0, i * 360 / spoke_count + 180 / spoke_count])
                    translate([hub_or - 1, -spoke_width / 2, H])
                        cube([ring_ir - hub_or + 2, spoke_width, lid_thickness]);
            // skirt
            translate([0, 0, H - skirt_height]) difference() {
                cylinder(r = skirt_or, h = skirt_height);
                translate([0, 0, -1]) cylinder(r = skirt_ir, h = skirt_height + 2);
            }
        }
        // neck hole
        translate([0, 0, H - 1]) cylinder(d = neck_hole_diameter, h = lid_thickness + 2);

        // bayonet: vertical entry channel + horizontal locking groove per lug
        for (i = [0 : lug_count - 1])
            rotate([0, 0, i * 360 / lug_count - chan_deg / 2]) {
                // entry channel, open at the skirt's bottom edge
                rotate_extrude(angle = chan_deg)
                    translate([skirt_ir - 1, H - skirt_height - 1])
                        square([groove_or - (skirt_ir - 1),
                                (lug_z1 + fit_clearance) - (H - skirt_height - 1)]);
                // locking groove, runs counter-clockwise from the channel
                rotate_extrude(angle = chan_deg + lock_rotation)
                    translate([skirt_ir - 1, lug_z0 - fit_clearance])
                        square([groove_or - (skirt_ir - 1), lug_height + 2 * fit_clearance]);
                // index groove on the outside of the skirt, at the channel
                if (index_marks)
                    rotate([0, 0, chan_deg / 2])
                        translate([skirt_or - 0.8, -0.75, H - skirt_height - 1])
                            cube([2, 1.5, skirt_height + 2]);
            }
    }
}

// lid flipped so its flat top face sits on Z=0: print it this way up
module lid_print() {
    rotate([180, 0, 0]) translate([0, 0, -(H + lid_thickness)]) lid_seated();
}

// ---- SPACER --------------------------------------------------------
module spacer() {
    difference() {
        cylinder(r = hub_or, h = spacer_height);
        translate([0, 0, -1]) cylinder(d = neck_hole_diameter, h = spacer_height + 2);
    }
}

// ---- OUTPUT --------------------------------------------------------
if (part == "body")          body();
else if (part == "lid")      lid_print();
else if (part == "spacer")   spacer();
else {                       // assembly, locked
    body();
    color("tomato") rotate([0, 0, -lock_rotation]) lid_seated();
}
