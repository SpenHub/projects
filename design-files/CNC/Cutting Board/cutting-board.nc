(Exported by FreeCAD)
(Post Processor: mach3_mach4_post)
(Output Time:2026-05-06 08:13:37.483420)
(begin preamble)
G17 G54 G40 G49 G80 G90 G90.1 G91.1
G20
(begin operation: Fixture)
(machine: mach3_4, in/min)
G54
(finish operation: Fixture)
(begin operation: TC: 3.175mm Endmill)
(machine: mach3_4, in/min)
(TC: 3.175mm Endmill)
M5
M6 T2 
G43 H2
(finish operation: TC: 3.175mm Endmill)
(begin operation: MillFace)
(machine: mach3_4, in/min)
(MillFace)
G0 Z2.1969
(finish operation: MillFace)
(begin postamble)
M05
G17 G54 G90 G80 G40
M2
