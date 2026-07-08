// Flexible decompiler: each arg is "off:0xNNNN" (decompile function containing
// that file-offset==vaddr) or "name:Symbol" (decompile that named function).
// For each function: prints signature, its callees (named), and decompiled C.
//@category Analysis

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.symbol.Reference;
import java.util.List;

public class DumpFuncs extends GhidraScript {
    DecompInterface dec;

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        long base = currentProgram.getImageBase().getOffset();
        println("IMAGE_BASE=0x" + Long.toHexString(base));
        dec = new DecompInterface();
        dec.openProgram(currentProgram);

        for (String a : args) {
            Function f = null;
            if (a.startsWith("off:")) {
                long off = Long.decode(a.substring(4));
                Address addr = currentProgram.getImageBase().getNewAddress(base + off);
                f = getFunctionContaining(addr);
                if (f == null) { println("\n!! no function contains " + a); continue; }
            } else if (a.startsWith("name:")) {
                List<Function> fs = getGlobalFunctions(a.substring(5));
                if (fs.isEmpty()) { println("\n!! no function named " + a); continue; }
                f = fs.get(0);
            } else {
                println("\n!! bad arg " + a); continue;
            }
            dumpFunc(f, base);
        }
        dec.dispose();
    }

    void dumpFunc(Function f, long base) {
        println("\n======== " + f.getName() + " @ " + f.getEntryPoint()
                + "  (fileoff 0x" + Long.toHexString(f.getEntryPoint().getOffset() - base) + ") ========");
        println("SIG: " + f.getPrototypeString(false, false));

        // callees, in address order, with the call-site offset
        println("---- calls (in order) ----");
        InstructionIterator it = currentProgram.getListing().getInstructions(f.getBody(), true);
        while (it.hasNext()) {
            Instruction ins = it.next();
            if (ins.getFlowType().isCall()) {
                StringBuilder sb = new StringBuilder();
                for (Reference r : ins.getReferencesFrom()) {
                    Function callee = getFunctionAt(r.getToAddress());
                    if (callee != null) sb.append(callee.getName()).append(" ");
                }
                long ioff = ins.getAddress().getOffset() - base;
                println("  0x" + Long.toHexString(ioff) + ": " + ins.toString()
                        + (sb.length() > 0 ? "   -> " + sb : ""));
            }
        }

        DecompileResults res = dec.decompileFunction(f, 120, monitor);
        println("---- decompiled C ----");
        if (res != null && res.decompileCompleted()) {
            println(res.getDecompiledFunction().getC());
        } else {
            println("!! decompile failed: " + (res == null ? "null" : res.getErrorMessage()));
        }
    }
}
