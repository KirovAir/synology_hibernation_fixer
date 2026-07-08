// Ghidra headless post-script: decompile the function containing each address
// passed as a script argument (hex file-offset == vaddr for these PIE binaries,
// so the Ghidra address is imageBase + offset).
//
// Usage (via analyzeHeadless): -postScript DecompilePatchSites.java 0x20104 [more...]
//@category Analysis

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.Reference;

public class DecompilePatchSites extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        long base = currentProgram.getImageBase().getOffset();
        println("IMAGE_BASE=0x" + Long.toHexString(base));

        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);

        for (String a : args) {
            long off = Long.decode(a);
            Address addr = currentProgram.getImageBase().getNewAddress(base + off);
            println("\n======== patch offset " + a + "  ->  ghidra addr " + addr + " ========");

            Function f = getFunctionContaining(addr);
            if (f == null) {
                println("!! no function contains this address (analysis may have missed it)");
                continue;
            }
            println("FUNCTION: " + f.getName() + "  entry=" + f.getEntryPoint()
                    + "  sig=" + f.getPrototypeString(false, false));

            // Identify the call target at/just before the patch site (the "check" function).
            Instruction ins = getInstructionAt(addr);
            for (int i = 0; ins != null && i < 4; i++) {
                if (ins.getFlowType().isCall()) {
                    for (Reference r : ins.getReferencesFrom()) {
                        Function callee = getFunctionAt(r.getToAddress());
                        if (callee != null) {
                            println("CALL at " + ins.getAddress() + " -> " + callee.getName()
                                    + " " + callee.getPrototypeString(false, false));
                        }
                    }
                    break;
                }
                ins = ins.getNext();
            }

            DecompileResults res = dec.decompileFunction(f, 90, monitor);
            if (res != null && res.decompileCompleted()) {
                println("---- decompiled C ----");
                println(res.getDecompiledFunction().getC());
            } else {
                println("!! decompile failed: " + (res == null ? "null" : res.getErrorMessage()));
            }
        }
        dec.dispose();
    }
}
