// Open-ended hibernation-logic survey for Synology scemd / synostoraged.
// Dumps named functions, every SYNODiskPortEnum call site (+ decompiled caller),
// and hibernation/idle/spindown/nvme strings with their referencing functions.
//@category Analysis

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.symbol.Reference;
import java.util.LinkedHashSet;

public class AnalyzeHibernation extends GhidraScript {
    @Override
    public void run() throws Exception {
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        long base = currentProgram.getImageBase().getOffset();
        println("PROGRAM " + currentProgram.getName() + "  imageBase=0x" + Long.toHexString(base));

        LinkedHashSet<Function> interesting = new LinkedHashSet<>();

        // 1) named (non-default) functions
        println("\n#### NAMED FUNCTIONS ####");
        int nf = 0;
        FunctionIterator fit = currentProgram.getFunctionManager().getFunctions(true);
        while (fit.hasNext()) {
            Function f = fit.next();
            String n = f.getName();
            if (!n.startsWith("FUN_") && !n.startsWith("thunk_FUN_") && !n.startsWith("_")) {
                println("  " + f.getEntryPoint() + "  " + n);
                nf++;
            }
        }
        println("  (" + nf + " named functions)");

        // 2) SYNODiskPortEnum call sites (the port-type checks)
        println("\n#### SYNODiskPortEnum CALL SITES ####");
        for (Function target : getGlobalFunctions("SYNODiskPortEnum")) {
            println("  target " + target.getName() + " @ " + target.getEntryPoint()
                    + (target.isThunk() ? " (thunk)" : ""));
            for (Reference r : getReferencesTo(target.getEntryPoint())) {
                Function caller = getFunctionContaining(r.getFromAddress());
                if (caller != null) {
                    println("    <- " + caller.getName() + " @ " + r.getFromAddress());
                    interesting.add(caller);
                }
            }
        }

        // 3) hibernation-related strings + referencing functions
        println("\n#### HIBERNATION / DISK-STATE STRINGS ####");
        String[] kw = {"hibern", "spindown", "spin down", "spin_down", "spinup", "standby",
                       "idle", "nvme", "diskport", "disk port", "deep sleep", "deepsleep",
                       "wake", "syno_hibernate", "polling_hibernation", "disk_monitor",
                       "poweroff", "no_hibernate", "disk_idle", "sleepstate"};
        int ns = 0;
        DataIterator dit = currentProgram.getListing().getDefinedData(true);
        while (dit.hasNext()) {
            Data d = dit.next();
            Object v = d.getValue();
            if (!(v instanceof String)) continue;
            String s = (String) v;
            String sl = s.toLowerCase();
            boolean match = false;
            for (String k : kw) { if (sl.contains(k)) { match = true; break; } }
            if (!match) continue;
            ns++;
            StringBuilder callers = new StringBuilder();
            for (Reference r : getReferencesTo(d.getAddress())) {
                Function cf = getFunctionContaining(r.getFromAddress());
                if (cf != null) { callers.append(cf.getName()).append(" "); interesting.add(cf); }
            }
            String disp = s.length() > 80 ? s.substring(0, 80) : s;
            println("  [" + d.getAddress() + "] \"" + disp.replace("\n", "\\n") + "\"  <- " + callers);
        }
        println("  (" + ns + " matching strings)");

        // 4) decompile the interesting functions
        println("\n#### DECOMPILED INTERESTING FUNCTIONS (" + interesting.size() + ") ####");
        int cap = 0;
        for (Function f : interesting) {
            if (cap++ >= 30) { println("\n  ...(capped at 30 functions)"); break; }
            println("\n----- " + f.getName() + " @ " + f.getEntryPoint()
                    + "  sig=" + f.getPrototypeString(false, false) + " -----");
            DecompileResults res = dec.decompileFunction(f, 60, monitor);
            if (res != null && res.decompileCompleted()) {
                println(res.getDecompiledFunction().getC());
            } else {
                println("  (decompile failed)");
            }
        }
        dec.dispose();
    }
}
