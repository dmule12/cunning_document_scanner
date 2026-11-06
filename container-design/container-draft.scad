// ICE-SLEEVE CONTAINER WITH BOTTLE HOLD-DOWN LID  (revision 3)
// Bottle: LENO 1200 ml / 42 oz single-wall stainless wide-mouth (Temu).
// ------------------------------------------------------------------
// PROVISIONAL DRAFT: body and bottle dimensions are estimates, NOT
// verified measurements (see README). Measure before relying on them.
//
// Problem: an empty wide-mouth steel bottle placed in the water-filled
// body floats. The bottle has no neck ring to hang from and its shoulder
// is near or above the rim, so the lid is a twist-lock ring on the body
// rim carrying a plate whose hole bears on the bottle's shoulder taper.
// If the shoulder is above the rim the plate is raised on a short
// "chimney" tube; if it is below, the plate sits flat on the ring.
// Buoyancy pushes the shoulder up into the plate and the bayonet lugs
// on the body stop the lid.
//
// Three openings in the plate (and side windows if the chimney is tall)
// let you top up water after locking and give the ice room to expand.
//
// Parts:  part = "body" | "lid" | "assembly"
//   body     - cylinder plus 3 external bayonet lugs (unchanged from rev 2)
//   lid      - chimney lid, output already flipped for printing (plate down)
//   assembly - body + locked lid + a translucent model of the bottle
//
// Regenerate STLs (from this directory):
//   openscad -D 'part="body"' -o container-body-127x180mm.stl  container-draft.scad
//   openscad -D 'part="lid"'  -o container-lid-chimney.stl     container-draft.scad
//
// To lock: lower the lid over the bottle with the index grooves on the
// skirt over the ribs on the body, press down onto the rim, twist
// CLOCKWISE (seen from above) by lock_rotation. Counter-clockwise releases.
// ------------------------------------------------------------------

part = "assembly"; // ["body", "lid", "assembly"]

// ---- BODY ----------------------------------------------------------
outside_diameter = 127;  // ESTIMATED; replace with measured base diameter
total_height     = 180;  // ESTIMATED; replace with measured base-to-red-line height
wall_thickness   = 2.4;
base_thickness   = 3;

// ---- BOTTLE: MEASURE THESE -----------------------------------------
// Defaults are for the LENO 1200 ml / 42 oz single-wall bottle, Temu listing
// size chart: W 3.6 in (91 mm), H 8.6 in (218 mm). The chart does not say
// whether the cap is included; 218 is taken as the height WITHOUT the cap,
// which is the safe assumption (a shorter bottle only floats up a bit more).
// Neck, shoulder and cap are guesses from the product photos. Stand the
// bottle on a table and measure:
bottle_body_diameter   = 91.4; // widest diameter of the straight body
bottle_neck_diameter   = 58;   // outside diameter across the threads
bottle_body_height     = 173;  // table to where the straight side ends and the shoulder starts curving in
bottle_shoulder_height = 25;   // vertical height of the shoulder taper, from there to the bottom of the neck
bottle_neck_height     = 20;   // neck/thread height (preview only)
bottle_cap_diameter    = 64;   // if you want to lock the lid with the cap on, hole_diameter must exceed this

// ---- LID -----------------------------------------------------------
hole_diameter      = 70;   // plate hole: bigger than the neck (or cap), smaller than the body
hub_width          = 8;    // solid ring kept around the hole between the fill openings
spoke_angle        = 16;   // angular width of the 3 spokes between the fill openings, degrees
plate_thickness    = 4;
hole_chamfer       = 2;    // 45 degree lead-in on the underside of the hole, seats on the shoulder
lift_margin        = 10;   // plate sits this much above where the shoulder would touch it with the
                           // bottle on the base; the bottle floats up this far and then stops.
                           // Measurement error is absorbed here: too big is harmless, too small
                           // means the lid cannot seat on the rim. Raise it if in doubt.
tube_thickness     = 2.5;
ring_thickness     = 4;    // thickness of the ring that sits on the rim
ring_inner_overlap = 2.6;  // how far the ring reaches in past the body's inner wall
window_count       = 3;
window_width       = 80;   // chord width of each window, mm
window_margin      = 8;    // solid band kept above/below the windows
skirt_height       = 16;
skirt_thickness    = 4;
fit_clearance      = 0.4;

// ---- BAYONET LUGS --------------------------------------------------
lug_count         = 3;
lug_height        = 3;
lug_protrusion    = 1.6;
lug_arc_width     = 8;
lug_top_below_rim = 7;
lock_rotation     = 25;
index_marks       = true;

$fn = 256;

// ---- derived -------------------------------------------------------
Ro        = outside_diameter / 2;
Ri        = Ro - wall_thickness;
H         = total_height;
skirt_ir  = Ro + fit_clearance;
skirt_or  = skirt_ir + skirt_thickness;
groove_or = skirt_ir + lug_protrusion + fit_clearance;
ring_ir   = Ri - ring_inner_overlap;        // also the chimney tube inner radius
tube_or   = ring_ir + tube_thickness;
lug_z1    = H - lug_top_below_rim;
lug_z0    = lug_z1 - lug_height;
lug_deg   = lug_arc_width / Ro * 180 / PI;
chan_deg  = (lug_arc_width + 2 * fit_clearance) / Ro * 180 / PI;

// bottle geometry with the bottle standing on the body's base
bR        = bottle_body_diameter / 2;
bn        = bottle_neck_diameter / 2;
hR        = hole_diameter / 2;
bz_body   = base_thickness + bottle_body_height;             // top of straight side
bz_neck   = bz_body + bottle_shoulder_height;                // bottom of neck
z_contact = bz_body + bottle_shoulder_height * (bR - hR) / (bR - bn); // where the hole edge meets the taper
ring_z1   = H + ring_thickness;
flare_h   = skirt_or - tube_or;                              // 45 degree flare from ring out to tube
chimney   = z_contact + lift_margin > ring_z1 + 1;           // shoulder above the rim: needs a chimney
plate_z0  = chimney ? z_contact + lift_margin : H;           // plate underside
plate_z1  = plate_z0 + plate_thickness;
plate_or  = chimney ? tube_or : skirt_or;
tube_h    = chimney ? plate_z0 - ring_z1 - flare_h : 0;
windows   = chimney && tube_h >= 2 * window_margin + 20;     // side windows only if the chimney is tall enough

assert(outside_diameter > 2 * wall_thickness);
assert(hR > bn + 1, "hole must clear the neck threads");
assert(hR < bR - 2, "hole must be smaller than the bottle body");
assert(bR + 2 < ring_ir, "bottle body too wide for the chimney");
assert(!chimney || tube_h >= 4, "chimney too short; raise lift_margin");
assert(skirt_thickness > lug_protrusion + fit_clearance + 1.5, "skirt too thin for groove");
assert(skirt_height > lug_top_below_rim + lug_height + 2, "skirt too short to reach lugs");
assert(lock_rotation + chan_deg < 360 / lug_count - 5, "grooves would run into each other");

echo(str(chimney ? "Chimney lid: " : "Flat lid: ", "plate underside ", plate_z0 - H, " mm above the rim; shoulder contact at ", z_contact - H, " mm relative to the rim with the bottle on the base; bottle floats up ", plate_z0 - z_contact, " mm before it stops."));

// ---- BODY ----------------------------------------------------------
module lug() {
    rotate([0, 0, -lug_deg / 2])
        rotate_extrude(angle = lug_deg)
            translate([Ro - 0.4, lug_z0]) square([0.4 + lug_protrusion, lug_height]);
}

module index_rib() {
    translate([Ro - 0.4, -0.75, H - skirt_height - 12]) cube([0.4 + 0.8, 1.5, 10]);
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

// ---- BOTTLE (preview only) ----------------------------------------
module bottle() {
    translate([0, 0, base_thickness])
        rotate_extrude()
            polygon([[0, 0], [bR, 0], [bR, bottle_body_height],
                     [bn, bottle_body_height + bottle_shoulder_height],
                     [bn, bottle_body_height + bottle_shoulder_height + bottle_neck_height],
                     [0, bottle_body_height + bottle_shoulder_height + bottle_neck_height]]);
}

// ---- LID (modelled seated on the body, unlocked, body coordinates) ----
module window_cut() {
    // rectangle with a 45 degree point at its lower end (the end that is on
    // top when printed plate-down), so it prints without support
    z0 = ring_z1 + flare_h + window_margin;
    z1 = plate_z0 - window_margin;
    hull() {
        translate([0, -window_width / 2, z0]) cube([ring_ir + 50, window_width, z1 - z0]);
        translate([0, -0.5, z0 - window_width / 2]) cube([ring_ir + 50, 1, 1]);
    }
}

module plate_opening() {
    // sector opening in the plate between the hub ring and the outer wall
    rotate([0, 0, spoke_angle / 2])
        rotate_extrude(angle = 360 / window_count - spoke_angle)
            translate([hR + hub_width, plate_z0 - 1])
                square([ring_ir - 3 - (hR + hub_width), plate_thickness + 2]);
}

module lid_seated() {
    difference() {
        union() {
            // ring on the rim
            translate([0, 0, H]) difference() {
                cylinder(r = skirt_or, h = ring_thickness);
                translate([0, 0, -1]) cylinder(r = ring_ir, h = ring_thickness + 2);
            }
            if (chimney) {
                // 45 degree flare from the ring's outer edge in to the tube
                translate([0, 0, ring_z1]) difference() {
                    cylinder(r1 = skirt_or, r2 = tube_or, h = flare_h);
                    translate([0, 0, -1]) cylinder(r = ring_ir, h = flare_h + 2);
                }
                // chimney tube
                translate([0, 0, ring_z1]) difference() {
                    cylinder(r = tube_or, h = plate_z0 - ring_z1);
                    translate([0, 0, -1]) cylinder(r = ring_ir, h = plate_z0 - ring_z1 + 2);
                }
            }
            // top plate (merges with the ring when there is no chimney)
            translate([0, 0, plate_z0]) cylinder(r = plate_or, h = plate_thickness);
            // skirt
            translate([0, 0, H - skirt_height]) difference() {
                cylinder(r = skirt_or, h = skirt_height);
                translate([0, 0, -1]) cylinder(r = skirt_ir, h = skirt_height + 2);
            }
        }
        // plate hole with a chamfer on its underside to sit on the shoulder taper
        translate([0, 0, plate_z0 - 1]) cylinder(r = hR, h = plate_thickness + 2);
        translate([0, 0, plate_z0 - 0.01]) cylinder(r1 = hR + hole_chamfer, r2 = hR, h = hole_chamfer);

        // fill openings in the plate, and side windows when the chimney is tall enough
        for (i = [0 : window_count - 1])
            rotate([0, 0, i * 360 / window_count + 180 / window_count]) {
                plate_opening();
                if (windows) window_cut();
            }

        // bayonet: vertical entry channel + locking groove per lug
        for (i = [0 : lug_count - 1])
            rotate([0, 0, i * 360 / lug_count - chan_deg / 2]) {
                rotate_extrude(angle = chan_deg)
                    translate([skirt_ir - 1, H - skirt_height - 1])
                        square([groove_or - (skirt_ir - 1),
                                (lug_z1 + fit_clearance) - (H - skirt_height - 1)]);
                rotate_extrude(angle = chan_deg + lock_rotation)
                    translate([skirt_ir - 1, lug_z0 - fit_clearance])
                        square([groove_or - (skirt_ir - 1), lug_height + 2 * fit_clearance]);
                if (index_marks)
                    rotate([0, 0, chan_deg / 2])
                        translate([skirt_or - 0.8, -0.75, H - skirt_height - 1])
                            cube([2, 1.5, skirt_height + 2]);
            }
    }
}

// flipped so the flat plate sits on Z=0: print it this way up, no supports
module lid_print() {
    rotate([180, 0, 0]) translate([0, 0, -plate_z1]) lid_seated();
}

// ---- OUTPUT --------------------------------------------------------
if (part == "body")       body();
else if (part == "lid")   lid_print();
else {
    body();
    color("tomato") rotate([0, 0, -lock_rotation]) lid_seated();
    %color("silver", 0.5) bottle();
}
