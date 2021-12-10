#!/usr/bin/env python

# blast2genomegff.py v1.0 2016-05-16
#
# and using information from:
# http://www.sequenceontology.org/gff3.shtml

# for SOFA terms:
# https://github.com/The-Sequence-Ontology/SO-Ontologies/blob/master/subsets/SOFA.obo

'''blast2genomegff.py  last modified 2021-12-10
    convert blast output to gff format for genome annotation
    blastx of a transcriptome (genome guided or de novo) against a protein DB:

blast2genomegff.py -b blastx_out6.tab -d protein_db.fasta -g transcripts.gtf > output.gff

    generate the tabular blastx output (-outfmt 6) by:
blastx -query transcripts.fasta -db protein_db.fasta -outfmt 6 -max_target_seqs 5 > blastx_out6.tab

    change the second and third fields in the gff output with -p and -t

blast2genomegff.py -b blastn_output.tab -p BLASTN -t EST_match > output.gff

    -t should be a standard sequence ontology term, including:
      nucleotide_match   EST_match   protein_match

    BLAST output should be made from blast programs using -outfmt 6

    evalue cutoff between 1e-3 and 1e-40 is appropriate to filter bad hits
    though this depends on the bitscore and so the relatedness of the species

    for AUGUSTUS GFF format genes, use -x and -T

    the reported score (column 6) is the bitscore

    for blastp, if GFF contains both exon and CDS features, use -x and -K
'''

import sys
import argparse
import time
import re
import os
import gzip
from collections import defaultdict,Counter
from itertools import chain
from Bio import SeqIO

def make_seq_length_dict(sequencefile, is_swissprot, get_description):
	sys.stderr.write("# Parsing target sequences from {}  ".format(sequencefile) + time.asctime() + os.linesep)
	lengthdict = {}
	otherdict = {}
	if get_description:
		sys.stderr.write("# Taking length and descriptions from sequences\n")
	for seqrec in SeqIO.parse(sequencefile,'fasta'):
		lengthdict[seqrec.id] = len(seqrec.seq)
		if get_description: #
			sseqid = seqrec.id
			if is_swissprot:
				sseqid = sseqid.split("|")[2]
			swissdescription = parse_swissprot_header(seqrec.description)
			otherdict[sseqid] = swissdescription
	sys.stderr.write("# Found {} sequences  ".format(len(lengthdict)) + time.asctime() + os.linesep)
	return lengthdict, otherdict

def get_max_frequency(intervals):
	'''from the list of intervals, return the highest frequency of any interval, to check if it is more than 1'''
	interval_counts = Counter(intervals)
	max_counts = max(interval_counts.values())
	return max_counts

def gtf_to_intervals(gtffile, keepcds, skipexons, transdecoder, nogenemode, genesplit):
	'''convert protein or gene intervals from gff to dictionary where mrna IDs are keys and lists of intervals are values'''
	# this should probably be a class
	geneintervals = defaultdict(list)
	gene_to_strand_dict = {} # key is ID, value is strand as str
	gene_to_scaffold_dict = {} # 

	commentlines = 0 # comment lines
	linecounter = 0 # all lines that are not comments, even if ignored later
	transcounter = 0 # counter for transcript or mRNA
	exoncounter = 0 # counter for exon or CDS
	ignoredfeatures = 0 # all other features that get ignored

	allowed_features = ["gene", "mRNA", "transcript", "exon", "CDS"]

	if gtffile.rsplit('.',1)[-1]=="gz": # autodetect gzip format
		opentype = gzip.open
		sys.stderr.write("# Parsing gff from {} as gzipped  ".format(gtffile) + time.asctime() + os.linesep)
	else: # otherwise assume normal open for fasta format
		opentype = open
		sys.stderr.write("# Parsing gff from {}  ".format(gtffile) + time.asctime() + os.linesep)
	if skipexons: #
		sys.stderr.write("# exon features WILL BE IGNORED\n")
	if keepcds: # alert user to the flags that have been set
		sys.stderr.write("# CDS features WILL BE USED as exons\n")
	if nogenemode:
		sys.stderr.write("# gene name and strand will be read for each exon\n")

	# begin parsing file
	for line in opentype(gtffile,'rt'):
		line = line.strip()
		if line: # ignore empty lines
			if line[0]=="#": # count comment lines, just in case
				commentlines += 1
			else:
				linecounter += 1
				lsplits = line.split("\t")
				scaffold = lsplits[0]
				feature = lsplits[2]
				strand = lsplits[6]
				attributes = lsplits[8]

				if feature not in allowed_features: # any other features may cause problems later
					ignoredfeatures += 1
					continue

				if attributes.find("ID")>-1: # indicates gff3 format
					geneid = re.search('ID=([\w.|-]+)', attributes).group(1)
				elif attributes.find("gene_id")>-1: # indicates gtf format
					geneid = re.search('transcript_id "([\w.|-]+)";', attributes).group(1)
				else:
					geneid = None

				if attributes.find("Parent")>-1: # gff3 format but no ID
					toplevel_ID = re.search('Parent=([\w.|-]+)', attributes).group(1)
				elif attributes.find("gene_id")>-1: # indicates gtf format
					toplevel_ID = re.search('gene_id "([\w.|-]+)";', attributes).group(1)
				else:
					toplevel_ID = None

				if geneid is None and toplevel_ID is None: # means no feature info was found, so error
					raise KeyError("ERROR: cannot extract ID or Parent from line {}\n{}\n".format(linecounter, line) )
				# if either geneid or toplevel_ID are missing, use the other
				if geneid is None and toplevel_ID is not None:
					geneid = toplevel_ID
				if geneid is not None and toplevel_ID is None:
					toplevel_ID = geneid

				# clean up transdecoder IDs
				if transdecoder: # meaning CDS IDs will start with cds.gene.123|m.1
					geneid = geneid.replace("cds.","") # simply remove the cds.
					geneid = geneid.replace(".cds","") # also works for AUGUSTUS
				# universally split all gene IDs
				if genesplit:
					geneid = geneid.rsplit(genesplit,1)[0]

				if feature=="transcript" or feature=="mRNA": # or (aqumode and feature=="gene"):
					transcounter += 1
					gene_to_strand_dict[geneid] = strand
					gene_to_scaffold_dict[geneid] = scaffold
				elif (feature=="exon" and not skipexons) or (keepcds and feature=="CDS"):
					exoncounter += 1
					boundaries = ( int(lsplits[3]), int(lsplits[4]) )
					if nogenemode: # gtf contains only exon and CDS, so get gene info from each CDS
						# strand and scaffold should be the same for each exon
						gene_to_strand_dict[toplevel_ID] = strand
						gene_to_scaffold_dict[toplevel_ID] = scaffold
					geneintervals[toplevel_ID].append(boundaries)
	sys.stderr.write("# Counted {} lines and {} comments  {}\n".format(linecounter, commentlines, time.asctime() ) )
	if ignoredfeatures:
		sys.stderr.write("# Ignored {} other features in the GFF\n".format(ignoredfeatures) )
	if transcounter:
		sys.stderr.write("# Counted {} exons for {} inferred transcripts\n".format(exoncounter, transcounter) )
	else: # no mRNA or transcript features were given, count was 0
		transcounter = len(gene_to_scaffold_dict)
		sys.stderr.write("# Counted {} exons for {} inferred transcripts\n".format(exoncounter, transcounter) )
	if exoncounter==0:
		sys.stderr.write("WARNING: NO suitable exons counted, check options -x or -G\n" )
	return geneintervals, gene_to_strand_dict, gene_to_scaffold_dict

def parse_tabular_blast(blastfile, lengthcutoff, evaluecutoff, bitscutoff, maxtargets, programname, outputtype, report_percent, donamechop, is_swissprot, seqlengthdict, descdict, get_accession, geneintervals, gene_to_strand_dict, gene_to_scaffold_dict, debugmode=False):
	'''parse blast hits from tabular blast and write each hit independently to stdout as genome gff'''
	querynamedict = defaultdict(int) # counter of unique queries
	# count results to filter
	not_found_subjects = 0 # counter for subject IDs not found by lookup
	shortRemovals = 0 # removals for lengthcutoff, by length of query, default is 0.1
	evalueRemovals = 0 # removals for evaluecutoff, default is 1e-3
	bitsRemovals = 0 # removals for bitscutoff, by bits per length, default is 0.1
	total_kept = 0
	# count frequency of other problems
	missingscaffolds = 0 # count if scaffold cannot be found, suggesting naming problem
	intervalproblems = 0 # counter if no intervals are found for some sequence
	duplicateintervals = 0 # counter if any queries have duplicate intervals
	maxremovals = 0 # counter for hits above max for each query
	# count other general stats
	intervalcounts = 0
	backframecounts = 0
	hitDictCounter = defaultdict(int)
	linecounter = 0
	accession = None

	# set up parameters by blast program
	blastprogram = programname.lower()
	if blastprogram=="blastn" or blastprogram=="blastx" or blastprogram=="tblastx":
		sys.stderr.write("# blast program is {}, assuming coordinates are nucleotides\n".format(blastprogram) )
		multiplier = 1
	else: # meaning blastp or tblastn
		sys.stderr.write("# blast program is {}, multiplying coordinates by 3\n".format(blastprogram) )
		multiplier = 3


	if blastfile.rsplit('.',1)[-1]=="gz": # autodetect gzip format
		opentype = gzip.open
		sys.stderr.write("# Starting BLAST parsing on {} as gzipped  ".format(blastfile) + time.asctime() + os.linesep)
	else: # otherwise assume normal open for fasta format
		opentype = open
		sys.stderr.write("# Starting BLAST parsing on {}  ".format(blastfile) + time.asctime() + os.linesep)
	for line in opentype(blastfile, 'r'):
		line = line.strip()
		if not line or line[0]=="#": # skip comment lines
			continue # also catch for empty line, which would cause IndexError
		linecounter += 1
		#qseqid, sseqid, pident, length, mismatch, gapopen, qstart, qend, sstart, send, evalue, bitscore		
		lsplits = line.split("\t")

		sseqid = lsplits[1]
		# do all removals before any real counting
		evalue = float(lsplits[10])
		bitscore = float(lsplits[11])
		alignlength = float(lsplits[3])
		hitstart = int(lsplits[6])
		hitend = int(lsplits[7])
		mismatches = lsplits[4] # keep as string, since it will only be used for printing later
		gapopens = lsplits[5] # as above

		# get length from length dict, otherwise return extremely large value, which would remove the hit
		subjectlength = seqlengthdict.get(sseqid,None)
		if subjectlength is None:
			not_found_subjects += 1
			continue
		fractioncov = alignlength / subjectlength
		bitslength = bitscore/alignlength
		# filter low quality matches
		if fractioncov < lengthcutoff: # skip domains that are too short
			shortRemovals += 1
			continue
		if bitslength < bitscutoff: # skip domains that are too short
			bitsRemovals += 1
			continue
		if evalue >= evaluecutoff: # skip domains with bad evalue
			evalueRemovals += 1
			continue
		total_kept += 1

		# then count queries
		qseqid = lsplits[0]
		if donamechop: # for transdecoder peptides, |m.123 is needed for interval identification
			qseqid = qseqid.rsplit(donamechop,1)[0]
		querynamedict[qseqid] += 1
		if is_swissprot:
		# blast outputs swissprot proteins as: sp|P0DI82|TPC2B_HUMAN
			if get_accession:
				accession = sseqid.split("|")[1] # should keep P0DI82
			sseqid = sseqid.split("|")[2] # should change to TPC2B_HUMAN
		else:
			sseqid = sseqid.replace("|","") ###TODO make this agree with seqlength dict
		hitDictCounter[sseqid] += 1

		# skip if there are already enough targets, default is 10
		# increment is several lines above, so must be greater than max
		if querynamedict.get(qseqid) > maxtargets:
			maxremovals += 1
			continue

		backframe = False
		if hitstart > hitend: # for cases where transcript has backwards hit
			hitstart, hitend = hitend, hitstart # invert positions for calculation
			backframe = True # also change the strand
			backframecounts += 1

		# convert protein positions to transcript nucleotide, as needed
		# protein position 1 becomes nucleotide position 1, position 2 becomes nucleotide 4, 3 to 7
		hitstart = (hitstart - 1) * multiplier + 1
		hitend = hitend * multiplier # end is necessarily the end of a codon
		hitlength = abs(hitend - hitstart) + 1 # bases 1 to 6 should have length 6
		scaffold = gene_to_scaffold_dict.get(qseqid, None)
		if scaffold is None:
			missingscaffolds += 1
			if missingscaffolds < 10:
				sys.stderr.write("WARNING: cannot get scaffold for {}\n".format( qseqid ) )
			elif missingscaffolds == 10:
				sys.stderr.write("WARNING: cannot get scaffold for {}, will not print further warnings\n".format( qseqid ) )
			continue
		strand = gene_to_strand_dict.get(qseqid, None)
		genomeintervals = [] # to have empty iterable

		# convert transcript nucleotide to genomic nucleotide, and split at exon bounds
		if strand=='+':
			genomeintervals = get_intervals(geneintervals[qseqid], hitstart, hitlength, doreverse = False )
		elif strand=='-': # implies '-'
			genomeintervals = get_intervals(geneintervals[qseqid], hitstart, hitlength, doreverse = True )
		elif strand=='.': # no strand is given by the input GFF
			sys.stderr.write("WARNING: strand is undefined . for {} on {}\n".format(qseqid, scaffold) )
			continue
		else: # strand is None
			# strand could not be found
			# meaning mismatch between query ID in blast and query ID in the GFF
			sys.stderr.write("WARNING: possible mismatch in ID for {} on {}\n".format(qseqid, scaffold) )
			continue

		# reassign feature strand if match is backwards
		# if gene is forward strand, and feature is backstranded, then assign as -
		# if gene is reverse strand, and feature is backstranded, assign as +
		if backframe: # meaning swap whatever the gene strand is
			strand = "+" if strand=="-" else "-"

		intervalcounts += len(genomeintervals)
		if not len(genomeintervals):
			sys.stderr.write("WARNING: no intervals for {} in {}\n".format(sseqid, qseqid) )
			intervalproblems += 1
			continue

		# check for duplicate intervals, often due to reading both exon and CDS features
		if get_max_frequency(genomeintervals) > 1:
			duplicateintervals += 1
			if duplicateintervals < 10:
				sys.stderr.write("WARNING: duplicate intervals found for {}, check option -x or -K\n".format( qseqid ) )
			elif duplicateintervals == 10:
				sys.stderr.write("WARNING: duplicate intervals found for {}, will not print further warnings\n".format( qseqid ) )

		# make Parent feature
		allpositions = list(chain(*genomeintervals))
		parentstart = min(allpositions)
		parentend = max(allpositions)

		# create attributes string
		target_sense_val = "-" if backframe else "+"
		is_same_sense_val = "0" if backframe else "1"
		if report_percent: # show target as percent, like CALM1_HUMAN 2.6 98.0 +
			S_env_start = float(lsplits[8]) * 100 / subjectlength
			S_env_end = float(lsplits[9]) * 100 / subjectlength
			parentattrs = "ID={1}.{0}.{2};Target={1} {3:.1f} {4:.1f} {5};same_sense={6}".format(qseqid, sseqid, hitDictCounter[sseqid], S_env_start, S_env_end, target_sense_val, is_same_sense_val)
		else: # show target as indices of the match protein
			parentattrs = "ID={1}.{0}.{2};Target={1} {3} {4} {5};same_sense={6}".format(qseqid, sseqid, hitDictCounter[sseqid], lsplits[8], lsplits[9], target_sense_val, is_same_sense_val)

		# add additional tags
		parentattrs += ";Gaps={};Mismatch={};Evalue={}".format(gapopens, mismatches, evalue)
		if descdict: # if making the description tag
			hitdescription = descdict.get(sseqid,"None")
			parentattrs += ";Description={}".format(hitdescription)
		if get_accession and accession is not None: # if adding accession
			parentattrs += ";Accession={}".format(accession)

		# final line to print
		outline = "{0}\t{1}\t{2}\t{3}\t{4}\t{5}\t{6}\t.\t{7}\n".format(scaffold, programname, outputtype, parentstart, parentend, bitscore, strand, parentattrs)
		sys.stdout.write( outline )

		# make child features for each interval
		for interval in genomeintervals:
		# thus ID appears as sseqid.qseqid.number, so avGFP.Renre1234.1, and uses ID in most browsers
			outline = "{0}\t{1}\tmatch_part\t{3}\t{4}\t{5}\t{6}\t.\tParent={8}.{7}.{9}\n".format(scaffold, programname, outputtype, interval[0], interval[1], bitscore, strand, qseqid, sseqid, hitDictCounter[sseqid] )
			sys.stdout.write( outline )
	sys.stderr.write("# Counted {} lines and kept {} hits\n".format(linecounter, total_kept) )
	if not_found_subjects:
		sys.stderr.write("# Could not find {} sequences in database, check -d or -D \n".format(not_found_subjects) )
	sys.stderr.write("# Removed {} hits by shortness\n".format(shortRemovals) )
	sys.stderr.write("# Removed {} hits by bitscore\n".format(bitsRemovals) )
	sys.stderr.write("# Removed {} hits by evalue\n".format(evalueRemovals) )
	sys.stderr.write("# Removed {} hits that exceeded query max\n".format(maxremovals) )
	sys.stderr.write("# Found {} hits for {} queries  {}\n".format(sum(hitDictCounter.values()), len(querynamedict), time.asctime() ) )
	if backframecounts:
		sys.stderr.write("# {} hits are antisense  ".format(backframecounts) + time.asctime() + os.linesep)
	if intervalcounts:
		sys.stderr.write("# Wrote {} match intervals  ".format(intervalcounts) + time.asctime() + os.linesep)
	else:
		sys.stderr.write("# WARNING: did not write any intervals, check options -D -F or -G for mismatch between IDs in GFF and blast table\n")
	if missingscaffolds:
		sys.stderr.write("# WARNING: could not find scaffold for {} hits  ".format(missingscaffolds) + time.asctime() + os.linesep)
	if intervalproblems:
		sys.stderr.write("# WARNING: {} matches have hits extending beyond gene bounds  ".format(intervalproblems) + time.asctime() + os.linesep)
	if duplicateintervals:
		sys.stderr.write("# WARNING: {} matches have duplicate intervals  ".format(duplicateintervals) + time.asctime() + os.linesep)
	# NO RETURN

def parse_swissprot_header(hitstring):
	# hitstring can conveniently be taken from seq_record.description
	# for example
	# swissprotdict.next().description
	# 'sp|Q6GZX4|001R_FRG3G Putative transcription factor 001R OS=Frog virus 3 (isolate Goorha) GN=FV3-001R PE=4 SV=1'
	#
	# from the swissprot website http://www.uniprot.org/help/fasta-headers
	# fasta headers appear as:
	# >db|UniqueIdentifier|EntryName ProteinName OS=OrganismName[ GN=GeneName]PE=ProteinExistence SV=SequenceVersion
	#
	# Where:
	#
    # db is 'sp' for UniProtKB/Swiss-Prot and 'tr' for UniProtKB/TrEMBL.
    # UniqueIdentifier is the primary accession number of the UniProtKB entry.
    # EntryName is the entry name of the UniProtKB entry.
    # ProteinName is the recommended name of the UniProtKB entry as annotated in the RecName field. For UniProtKB/TrEMBL entries without a RecName field, the SubName field is used. In case of multiple SubNames, the first one is used. The 'precursor' attribute is excluded, 'Fragment' is included with the name if applicable.
    # OrganismName is the scientific name of the organism of the UniProtKB entry.
    # GeneName is the first gene name of the UniProtKB entry. If there is no gene name, OrderedLocusName or ORFname, the GN field is not listed.
    # ProteinExistence is the numerical value describing the evidence for the existence of the protein.
    # SequenceVersion is the version number of the sequence.
	#
	# Examples:
	#
	# >sp|Q8I6R7|ACN2_ACAGO Acanthoscurrin-2 (Fragment) OS=Acanthoscurria gomesiana GN=acantho2 PE=1 SV=1
	# or
	# sp|Q9GZU7|CTDS1_HUMAN Carboxy-terminal domain RNA polymerase II polypeptide A small phosphatase 1 OS=Homo sapiens GN=CT DSP1 PE=1 SV=1

	genedescRE = "(.+) OS="
	# split will take everything after the first space, so:
	# "Carboxy-terminal domain RNA polymerase II polypeptide A small phosphatase 1 OS=Homo sapiens GN=CT DSP1 PE=1 SV=1"
	nospnumber = hitstring.split(' ',1)[1]
	# this should pull everything before the species excluding " OS=", so:
	# "Carboxy-terminal domain RNA polymerase II polypeptide A small phosphatase 1"
	genedesc = re.search(genedescRE, nospnumber).groups()[0]
	# this should specifically remove the annotation "(Fragment)" for some proteins
	genedesc = genedesc.replace("(Fragment)","")
	genedesc = genedesc.replace("3'","3-prime").replace("5'","5-prime")
	genedesc = genedesc.replace("G(s)", "G_s")
	genedesc = genedesc.replace("G(q)", "G_q")
	genedesc = genedesc.replace("G(k)", "G_k")
	genedesc = genedesc.replace(" [GTP]","")
	genedesc = genedesc.replace(" [ubiquinone]","")
	genedesc = genedesc.replace(" [glutamine-hydrolyzing]","")

	# change a bunch of disallowed symbols
	underscore_symbols = "(),"
	for symbol in underscore_symbols:
		genedesc = genedesc.replace(symbol,"_")
	remove_symbols = "'[]"
	for symbol in underscore_symbols:
		genedesc = genedesc.replace(symbol,"")
	genedesc = genedesc.replace("/","-")

	# return description
	return genedesc

def get_intervals(intervals, domstart, domlength, doreverse=True):
	'''return a list of intervals with genomic positions for the feature'''
	# example domain arrangement for forward strand
	# intervals from     50,101 127,185 212,300
	# protein domain     71,101 127,185 212,256
	#      in nucleotides  31      59      45
	#      in amino acids  10.3    19.6    15 = 45
	# for domstart at 22 and domlength of 135
	# basestostart is always from transcript N-terminus nucleotide
	# so for forward transcripts, basestostart would be 22, so that 50+22-1=71
	basestostart = int(domstart) # this value always should be 1 or greater
	genomeintervals = [] # will contain a list of tuples
	for interval in sorted(intervals, key=lambda x: x[0], reverse=doreverse):
		intervallength = interval[1]-interval[0]+1 # corrected number of bases
		if basestostart >= intervallength: # ignore intervals before the start of the domain
		#	sys.stderr.write(" ".join([interval, domstart, basestostart, domlength, intervallength]))
			basestostart -= intervallength
		# in example, 101-50+1 = 52, 22 < 52, so else
		else: # bases to start is fewer than length of the interval, meaning domain must start here
			if doreverse: # reverse strand domains
				# if domain continues past an interval, domstart should be equal to interval[1]
				domstart = interval[1] - basestostart + 1 # correct for base numbering at end of interval
				if domstart - interval[0] + 1 >= domlength: # if the remaining part of the domain ends before the start of the interval
					# then define the last boundary and return the interval list
					genomebounds = (domstart-domlength+1, domstart) # subtract remaining length
					genomeintervals.append(genomebounds)
					return genomeintervals
				else:
					genomebounds = (interval[0], domstart)
					genomeintervals.append(genomebounds)
					domlength -= (domstart - interval[0] + 1)
					basestostart = 1 # start at the next interval
			else: # for forward stranded domains
				domstart = interval[0] + basestostart - 1 # correct for base numbering
				if interval[1] - domstart + 1 >= domlength: # if the remaining part of the domain ends before the end of the interval
					# then define the last boundary and return the interval list
					genomebounds = (domstart, domstart+domlength-1) # add remaining length for last interval
					genomeintervals.append(genomebounds)
					return genomeintervals
				else:
					genomebounds = (domstart, interval[1])
					genomeintervals.append(genomebounds)
					domlength -= (interval[1] - domstart + 1)
					basestostart = 1 # next domstart should be interval[0] for next interval
			if domlength < 1: # catch for if all domain length is accounted for
				return genomeintervals
	else:
		sys.stderr.write("WARNING: cannot finish protein at {} for {} in {}\n".format(domstart, domlength, intervals) )
		return genomeintervals

def main(argv, wayout):
	if not len(argv):
		argv.append("-h")
	parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
	parser.add_argument('-b','--blast', help="tabular blast results file, can be .gz")
	parser.add_argument('-d','--database', help="db (reference/subject) proteins in fasta format")
	parser.add_argument('-g','--genes', help="query genes or proteins in gff format, can be .gz")
	parser.add_argument('-p','--program', help="blast program for 2nd column in output [BLASTX]", default="BLASTX")
	parser.add_argument('-t','--type', help="gff type or method [protein_match]", default="protein_match")
	parser.add_argument('-D','--blast-delimiter', help="optional delimiter for query protein names in blast table, cuts off end split")
	parser.add_argument('-F','--gff-delimiter', help="optional delimiter for GFF gene IDs, cuts off end split")
	parser.add_argument('-c','--coverage-cutoff', type=float, help="query coverage cutoff for filtering [0.1]", default=0.1)
	parser.add_argument('-e','--evalue-cutoff', type=float, help="evalue cutoff [1e-3]", default=1e-3)
	parser.add_argument('-s','--score-cutoff', type=float, help="bitscore/length cutoff for filtering [0.1]", default=0.1)
	parser.add_argument('-M','--max-targets', type=int, help="most targets to allow per query [10]", default=10)
	parser.add_argument('-G','--no-genes', action="store_true", help="genes are not defined, get gene ID for each exon")
	parser.add_argument('-P','--percent-target', action="store_true", help="print Target tag as percent of target protein, instead of coordinates")
	parser.add_argument('-S','--swissprot', action="store_true", help="subject db sequences have swissprot headers in blast table")
	parser.add_argument('--add-description', action="store_true", help="if using swissprot, make GFF attribute of description from the protein description")
	parser.add_argument('--add-accession', action="store_true", help="if using swissprot, include accession in attribute, for downstream linking")
	parser.add_argument('-T','--transdecoder', action="store_true", help="use presets for TransDecoder genome gff")
	parser.add_argument('-x','--cds-exons', action="store_true", help="use CDS features as exons")
	parser.add_argument('-K','--skip-exons', action="store_true", help="skip exon features if exon and CDS are in the same file")
	parser.add_argument('-v','--verbose', action="store_true", help="extra output")
	args = parser.parse_args(argv)

	# read database, make a length dict, and possibly also a description dict
	if args.database is not None and os.path.exists(args.database):
		protlendb, descdict = make_seq_length_dict(args.database, args.swissprot, args.add_description)
	else:
		sys.exit("ERROR: cannot find database file -d {}, exiting".format(args.database) )

	# read the GFF
	geneintervals, gene_to_strand_dict, gene_to_scaffold_dict =  gtf_to_intervals(args.genes, args.cds_exons, args.skip_exons, args.transdecoder, args.no_genes, args.gff_delimiter)

	# read the blast output
	parse_tabular_blast(args.blast, args.coverage_cutoff, args.evalue_cutoff, args.score_cutoff, args.max_targets, args.program, args.type, args.percent_target, args.blast_delimiter, args.swissprot, protlendb, descdict, args.add_accession, geneintervals, gene_to_strand_dict, gene_to_scaffold_dict)

if __name__ == "__main__":
	main(sys.argv[1:],sys.stdout)
