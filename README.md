# LLM-SGDL-Generation

This repository handles generation of Solitaire GDLs using kubernetes, and its itegration with LLM generation. The main script runs kubernetes jobs for randomized generation, evaluation and mutation of SGDL files. The LLM generation can be done in a single step, or by mutating an existing SGDL, possibly with accumulation of skills through this process.

The repository is meant to run iterative generate-and-evaluate process using sgdl scripts implemented at https://github.com/iambb5445/SolitaireGDL/tree/main/job_scripts.