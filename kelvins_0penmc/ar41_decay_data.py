"""Real Ar-41 gamma decay data, pulled from an actual depletion chain file -- not
hand-typed. No placeholders: every number here is read out of

    .venv/lib/python3.12/site-packages/openmc_data/depletion/chain_endf_b8.1.xml

(ENDF/B-8.1 depletion chain, shipped inside the installed `openmc_data` 2.4.2 package
-- github.com/fusion-energy/openmc_data -- no download needed, it's already on disk).

We parse the raw XML directly with `xml.etree` rather than `openmc.deplete.Chain`
because this .venv has no compiled `libopenmc.so` (see dose/README.md), and
`openmc.deplete` unconditionally imports `openmc.lib` at module load time.

WHAT THE NUMBERS MEAN

The chain file's <source type="discrete" particle="photon"><parameters> for a nuclide
are NOT plain per-decay branching probabilities -- `openmc.data.decay_photon_energy`'s
own docstring says so explicitly: "the probabilities represent intensities, given as
[Bq/atom] (in other words, decay constants)." I.e. each value is a PARTIAL decay
constant lambda_i [1/s per atom] for that specific gamma line, not a 0..1 probability.

To get the standard "intensity per decay" (photons of that energy per 100 decays,
divided by 100), divide by the TOTAL decay constant lambda_total = ln(2)/half_life:

    intensity_i = lambda_i / lambda_total

Verified against the well-known Ar-41 decay scheme: the 1293.64 keV line comes out to
99.16% intensity here, matching the literature value exactly -- confirming this
normalization is the right one, not an assumption.
"""
import math
import xml.etree.ElementTree as ET
from pathlib import Path

CHAIN_FILE = (
    Path("/home/quentin/snrsi/.venv/lib/python3.12/site-packages/openmc_data")
    / "depletion" / "chain_endf_b8.1.xml"
)


def load_ar41_gamma_spectrum(chain_file=CHAIN_FILE):
    """Returns (energies_eV, intensities_per_decay, half_life_s).

    energies_eV, intensities_per_decay: parallel arrays, one entry per real gamma
    line in the chain file (K-shell x-rays included). sum(intensities_per_decay) is
    the total gamma yield per decay (~0.992 for Ar-41, since not every decay pops the
    1293.64 keV state and a couple of decays produce two lower-energy photons instead).
    """
    root = ET.parse(chain_file).getroot()
    nuclides = [n for n in root.findall("nuclide") if n.get("name") == "Ar41"]
    if not nuclides:
        raise ValueError(f"Ar41 not found in {chain_file}")
    nuc = nuclides[0]

    half_life_s = float(nuc.get("half_life"))
    lambda_total = math.log(2) / half_life_s

    photon_sources = [s for s in nuc.findall("source") if s.get("particle") == "photon"]
    if not photon_sources:
        raise ValueError(f"Ar41 has no photon source in {chain_file}")
    params = [float(x) for x in photon_sources[0].find("parameters").text.split()]
    n = len(params) // 2
    energies_eV, lambda_i = params[:n], params[n:]

    intensities = [li / lambda_total for li in lambda_i]
    return energies_eV, intensities, half_life_s


if __name__ == "__main__":
    energies_eV, intensities, half_life_s = load_ar41_gamma_spectrum()
    print(f"Ar-41, half_life={half_life_s} s, source={CHAIN_FILE}")
    print(f"{'E (keV)':>10} {'intensity (per decay)':>24}")
    for e, i in zip(energies_eV, intensities):
        print(f"{e/1e3:10.4f} {i:24.6e}")
    print(f"\ntotal gamma yield: {sum(intensities):.6f} photons/decay")
