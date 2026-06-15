# Family-based analysis and variational programs: a literature grounding

Status: research note (input to the config / flag-world design)
Audience: anyone working on flag-world analysis who wants the primary sources behind the theory. The design that uses this is [`config-and-flag-worlds.md`](../config-and-flag-worlds.md).

This note collects the verified literature behind treating a dbt project's flag space as a software product line and analyzing it family-based (one shared pass over all configurations) rather than product-based (one run per configuration). Bibliographic details were verified against dblp and ACM/Springer; a few attribution corrections found during verification are called out. Where a paper was read from the local `reading_room` corpus the quotes are from source; the rest were verified on the web.

A note on the corpus: the local DB/PL corpus holds the abstract-interpretation and dataflow foundations (Cousot and Cousot 1979, ASTRÉE, IFDS) and the provenance-semiring line, but **none** of the software-product-line variability literature and **no** SQL or data-pipeline lifted-analysis work. That second absence is itself a finding (see "The gap").

## The taxonomy to adopt

- **Thomas Thüm, Sven Apel, Christian Kästner, Ina Schaefer, Gunter Saake. "A Classification and Survey of Analysis Strategies for Software Product Lines." ACM Computing Surveys 47(1), Article 6, 2014.** DOI 10.1145/2580950. The canonical survey and the right vocabulary: **product-based** (analyze each generated product, or a sample, separately; the brute-force baseline, exponential in features), **feature-based** (analyze feature modules in isolation; misses interactions), **family-based** (one pass over the whole code base plus the variability model, carrying presence conditions, covering all valid products). dblect's flag-world analysis is family-based; the climb from re-compilation toward a lifted pass is a climb from product-based to family-based.

## The lifted analysis machinery (the spine)

- **Eric Bodden, Társis Tolêdo, Márcio Ribeiro, Claus Brabrand, Paulo Borba, Mira Mezini. "SPLLIFT: Statically Analyzing Software Product Lines in Minutes Instead of Years." PLDI 2013.** DOI 10.1145/2491956.2491976 (the SIGPLAN Notices 48(6) listing also appears as 10.1145/2499370.2491976). The flagship result: transparently convert any IFDS dataflow analysis into an IDE analysis whose edge functions carry, per dataflow fact, the **presence condition** (the set of configurations in which the fact holds) as a minimized BDD over feature variables. One fixpoint computes every configuration's result, "without changing a single line" of the underlying analysis. The presence-condition output is exactly the "which worlds break this" answer.

- **Claus Brabrand, Márcio Ribeiro, Társis Tolêdo, Paulo Borba. "Intraprocedural Dataflow Analysis for Software Product Lines." AOSD 2012; extended (with Johnni Winther) in TAOSD IX, LNCS 7271, 2012.** DOI 10.1145/2162049.2162052. The **lifted lattice** and the strategy spectrum from feature-oblivious brute force (A0) through feature-sensitive consecutive, simultaneous, shared/tagged, and combined strategies (A1 to A4), reported up to roughly 8x faster than brute force. The unifying idea: lift the base lattice to functions from configurations to base values and lift the transfer functions pointwise; share across configurations that agree. (Per-strategy one-liners are paraphrase; the PDF body resisted clean extraction, but the A0 to A4 naming and the "proven equivalent" claim are confirmed across the abstract and secondary sources.)

- **Jan Midtgaard, Aleksandar S. Dimovski, Claus Brabrand, Andrzej Wąsowski. "Systematic Derivation of Correct Variability-Aware Program Analyses." Science of Computer Programming 105, 2015.** DOI 10.1016/j.scico.2015.04.005. Derive the lifted analysis from the single-program one by calculation, so each lifting step is a sound abstraction and the result is correct by construction. The soundness argument for "we lifted our existing per-world analysis."

- **Aleksandar S. Dimovski, Claus Brabrand, Andrzej Wąsowski. "Variability Abstractions: Trading Precision for Speed in Family-Based Analyses." ECOOP 2015** (LIPIcs 37, 247-270); **"Efficient Family-Based Model Checking via Variability Abstractions" (with Ahmad Salim Al-Sibahi), STTT 19(5), 2017.** DOI 10.1007/s10009-016-0425-2. A variability abstraction is a Galois connection over the configuration space that soundly collapses feature distinctions a check cannot distinguish, refinable when precision is needed. The principled form of per-contract scoping and DAG factoring.

- **Aleksandar S. Dimovski, Sven Apel, Axel Legay. "A Decision Tree Lifted Domain for Analyzing Program Families with Numerical Features." FASE 2021** (LNCS 12649, 67-86; extended arXiv:2012.05863). Decision-tree lifted domain: inner nodes decide over feature expressions (including linear constraints over numerical features), leaves hold a base abstract value, sharing across configuration regions that agree. The representation to reach for when flags are numeric or enum rather than boolean. *Attribution correction:* the third author is Axel Legay, not Wąsowski.

## Variability-aware parsing (preserving the branches)

- **Christian Kästner, Paolo G. Giarrusso, Tillmann Rendel, Sebastian Erdweg, Klaus Ostermann, Thorsten Berger. "Variability-Aware Parsing in the Presence of Lexical Macros and Conditional Compilation." OOPSLA 2011** (TypeChef). DOI 10.1145/2048066.2048128. Parse unpreprocessed C, conditionals and macros intact, into one AST whose forks are **choice nodes** guarded by presence conditions, no per-configuration blow-up. The direct analog of parsing Jinja-templated SQL while keeping `{% if var %}` instead of committing to a branch, including the warning that preprocessor or interpolation variation straddling token boundaries breaks a naive lex-then-parse pipeline.

- **Paul Gazzillo, Robert Grimm. "SuperC: Parsing All of C by Taming the Preprocessor." PLDI 2012.** DOI 10.1145/2254064.2254103. **Fork-merge LR parsing**: fork the parser at a static conditional, run a subparser per branch carrying its presence condition, merge when they reconverge to the same parser state. The implementable engine for a variation-preserving front end (fork at `{% if %}`, merge at `{% endif %}`).

- **Andy Kenner, Christian Kästner, Steffen Haase, Thomas Leich. "TypeChef: Toward Type Checking #ifdef Variability in C." FOSD 2010 workshop** (co-located with GPCE/SPLASH; dblp indexes it under the gpce stream, but the venue is the FOSD workshop). DOI 10.1145/1868688.1868693. The motivation for checking all configurations at once.

## The formal calculus and data structures of variation

- **Martin Erwig, Eric Walkingshaw. "The Choice Calculus: A Representation for Software Variation." ACM TOSEM 21(1), Article 6, 2011.** DOI 10.1145/2063239.2063245. A choice `D⟨a,b⟩` bound to a dimension `D` selects between alternatives; selecting a tag per dimension projects the variational artifact to one variant. The formalism behind choice nodes and the laws for manipulating them.

- **Eric Walkingshaw, Christian Kästner, Martin Erwig, Sven Apel, Eric Bodden. "Variational Data Structures: Exploring Tradeoffs in Computing with Variability." Onward! 2014.** DOI 10.1145/2661136.2661143. Data structures that hold many variants at once and share common parts, so computation over the family runs once and specializes lazily. The data-structure underpinning of "the analysis state under all worlds" as one shared object rather than a list of per-world states.

## Presence conditions and "which configurations break it"

- **Sahil Thaker, Don Batory, David Kitchin, William Cook. "Safe Composition of Product Lines." GPCE 2007.** DOI 10.1145/1289971.1289989. Express each potential error as a propositional constraint over features, conjoin with the feature model, and discharge with SAT: a property holds for all products iff the formula is unsatisfiable. The SAT-over-flags view of whole-family verification.

- **Krzysztof Czarnecki, Krzysztof Antkiewicz. "Mapping Features to Models: A Template Approach Based on Superimposed Variants." GPCE 2005** (LNCS 3676), and **Czarnecki, Pietroszek. "Verifying Feature-Based Model Templates Against Well-Formedness OCL Constraints." GPCE 2006**, DOI 10.1145/1173706.1173738. The canonical articulation of a **presence condition** as a boolean formula over features annotating a superimposed template.

- **Reinhard Tartler, Daniel Lohmann, Julio Sincero, Wolfgang Schröder-Preikschat. "Feature Consistency in Compile-Time-Configurable System Software: Facing the Linux 10,000 Feature Problem." EuroSys 2011.** DOI 10.1145/1966445.1966451. Extract presence conditions from Kconfig, `#ifdef`, and Kbuild, then SAT-check cross-space consistency (dead and undead blocks) at kernel scale (the `undertaker` toolchain). Evidence the family-based approach scales to industrial configurability, and the closest real-world precedent for reasoning across thousands of compile-time flags.

## Foundations (from the local corpus, read directly)

- **Patrick Cousot, Radhia Cousot. "Abstract Interpretation: A Unified Lattice Model..." POPL 1977** (DOI 10.1145/512950.512973); **"Systematic Design of Program Analysis Frameworks." POPL 1979** (DOI 10.1145/567752.567778). The sound-by-construction foundation, and the formalization of an analysis framework as a parameter you instantiate, which is the posture dblect's one `propagate` already takes before lifting over a family.

- **Bruno Blanchet, Patrick Cousot, Radhia Cousot, Jérôme Feret, Laurent Mauborgne, Antoine Miné, David Monniaux, Xavier Rival. ASTRÉE: "A Static Analyzer for Large Safety-Critical Software." PLDI 2003.** The "design a parametrizable analyzer, then adapt it to a family of related programs" precedent. Worth a distinction in the doc: ASTRÉE's "family" is a domain class of similar programs the analyzer is tuned to, not one variational program with explicit flags. Cite it for the philosophy and the soundness-at-scale evidence; cite SPLLIFT and the Brabrand and Dimovski line for analyzing one variational program across all its configurations.

- **Thomas Reps, Susan Horwitz, Mooly Sagiv. "Precise Interprocedural Dataflow Analysis via Graph Reachability" (IFDS). POPL 1995** (DOI 10.1145/199448.199462); **Sagiv, Reps, Horwitz, IDE extension, TCS 1996.** The distributive interprocedural dataflow framework SPLLIFT lifts. The distributivity requirement is the test for which checks lift cleanly.

## Tangential but transplantable

- **Ramy Shahin, Marsha Chechik. "Lifting Datalog-Based Analyses to Software Product Lines" (Variability-Aware Datalog). ESEC/FSE 2019** (DOI 10.1145/3338906.3338928; related arXiv:1912.03854). Lift the Datalog engine, not each query, so any analysis expressible in Datalog (reachability, points-to, taint, def-use) becomes configuration-aware in one evaluation. Directly relevant since column lineage and taint are commonly Datalog-shaped.

- **Iago Abal, Claus Brabrand, Andrzej Wąsowski. "42 Variability Bugs in the Linux Kernel: A Qualitative Analysis." ASE 2014.** DOI 10.1145/2642937.2642990. Motivating evidence that real defects surface only at specific feature combinations and are invisible to any single-configuration build, the cross-configuration analog of dblect's one-world blindness.

## The gap

A deliberate search of both the web and the local corpus found **no established family-based or lifted static-analysis work for SQL, data pipelines, or templated SQL specifically.** The techniques are mature (variability-aware parsing, IFDS-to-IDE lifting, presence conditions, variational data structures, variability-aware Datalog); their application to Jinja-templated dbt SQL is open territory. The honest and generous framing for the design: build on the software-product-line community's foundations and carry them to a setting they have not yet covered.

## Attribution corrections noted during verification

- "Safe Composition of Product Lines" authors are Thaker, Batory, Kitchin, Cook (no "Gokhale").
- The decision-tree lifted domain's third author is Axel Legay, not Wąsowski.
- TypeChef FOSD 2010 is the FOSD workshop co-located with GPCE/SPLASH, not GPCE proper, despite dblp's gpce stream key.
- The presence-condition mechanism originates with Czarnecki and Antkiewicz (GPCE 2005); the SAT-style verification framing is Czarnecki and Pietroszek (GPCE 2006).
</content>
