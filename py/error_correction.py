import itertools as it
from multiprocessing import Pool

import pandas as pd
import numpy as np
from scipy.stats import fisher_exact
import pysam

from .sam_fasta_converter import SAMFASTAConverter


def grouper(iterable, n, fillvalue=None):
    args = [iter(iterable)] * n
    return it.zip_longest(*args, fillvalue=fillvalue)


def partial_covariation_test(arguments):
    pysam_path, pairs, i = arguments
    pairs, pairs_for_max, pairs_for_min = it.tee(
        it.filterfalse(lambda x: x is None, pairs),
        3
    )
    print('   ...processing block %d...' % i)
    pysam_alignment = pysam.AlignmentFile(pysam_path, 'rb')
    pair_min = min([min(pair[0], pair[1]) for pair in pairs_for_min])
    pair_max = max([max(pair[0], pair[1]) for pair in pairs_for_max])
    sfc = SAMFASTAConverter()
    fasta_window = sfc.sam_window_to_fasta(pysam_alignment, pair_min, pair_max+1)
    results = []
    for col_i, col_j in pairs:
        idx_i = col_i - pair_min
        idx_j = col_j - pair_min
        i_char_counts = pd.Series(
            fasta_window[:, idx_i]
        ).value_counts().drop('~', errors='ignore')
        i_chars = i_char_counts.index[i_char_counts > 0]

        j_char_counts = pd.Series(
            fasta_window[:, idx_j]
        ).value_counts().drop('~', errors='ignore')
        j_chars = j_char_counts.index[j_char_counts > 0]

        content_i = fasta_window[:, idx_i] != '~'
        content_j = fasta_window[:, idx_j] != '~'
        valid = content_i & content_j
        if valid.sum() == 0:
            continue
        for i_char, j_char in it.product(i_chars, j_chars):
            equals_i = fasta_window[valid, idx_i] == i_char
            equals_j = fasta_window[valid, idx_j] == j_char
            X_11 = (equals_i & equals_j).sum()
            X_21 = (~equals_i & equals_j).sum()
            X_12 = (equals_i & ~equals_j).sum()
            X_22 = (~equals_i & ~equals_j).sum()
            
            table = [
                [X_11 , X_12],
                [X_21 , X_22]
            ]
            _, p_value = fisher_exact(table)
            results.append((col_i, col_j, i_char, j_char, p_value))
    pysam_alignment.close()
    print('   ...done block %d!' % i)
    return pd.DataFrame(
        results, columns=('col_i', 'col_j', 'i_char', 'j_char', 'p_value')
    )


class ErrorCorrection:
    def __init__(self, pysam_alignment, all_fe_tests=None):
        self.pysam_alignment = pysam_alignment
        self.reference_length = pysam_alignment.header['SQ'][0]['LN']
        self.number_of_reads = 0
        for read in pysam_alignment.fetch():
            self.number_of_reads += 1

        if all_fe_tests:
            self.all_fe_tests = pd.read_csv(all_fe_tests)
        else:
            self.all_fe_test = None
        self.covarying_sites = None
        self.pairs = None
        self.nucleotide_counts = None

    @staticmethod
    def read_count_data(read):
        sequence_length = np.array([
            cigar_tuple[1]
            for cigar_tuple in read.cigartuples
            if cigar_tuple[0] != 1
        ]).sum()
        first_position = read.reference_start
        last_position = first_position + sequence_length
        positions = np.arange(first_position, last_position)
        segments = []
        number_of_cigar_tuples = len(read.cigartuples)
        unaligned_sequence = read.query_alignment_sequence
        position = 0
        for i, cigar_tuple in enumerate(read.cigartuples):
            action = cigar_tuple[0]
            stride = cigar_tuple[1]
            match = action == 0
            insertion = action == 1
            deletion = action == 2
            if match:
                segments.append(unaligned_sequence[position: position + stride])
                position += stride
            elif insertion:
                position += stride
            elif deletion:
                if len(segments) > 0 and i < number_of_cigar_tuples:
                    segments.append(stride * '-')
        sequence = np.concatenate([list(segment) for segment in segments])
        return sequence, positions

    def get_nucleotide_counts(self):
        if not self.nucleotide_counts is None:
            return self.nucleotide_counts
        print('Calculating nucleotide counts...')
        characters = ['A', 'C', 'G', 'T', '-']
        counts = np.zeros((self.reference_length, 5))
        for read in self.pysam_alignment.fetch():
            sequence, positions = self.read_count_data(read)
            for character_index, character in enumerate(characters):
                rows = positions[sequence == character]
                counts[rows, character_index] += 1

        df = pd.DataFrame(counts, columns=characters)
        zeros = lambda character: (df[character] == 0).astype(np.int)
        zero_cols = zeros('A') + zeros('C') + zeros('G') + zeros('T')
        df['interesting'] = zero_cols < 3
        df['nucleotide_max'] = df[['A', 'C', 'G', 'T']].max(axis=1)
        df['consensus'] = '-'
        for character in characters[:-1]:
            df.loc[df['nucleotide_max'] == df[character], 'consensus'] = character
        self.nucleotide_counts = df
        return df

    def get_pairs(self):
        if self.pairs:
            return self.pairs
        counts = self.get_nucleotide_counts()
        interesting = counts.index[counts.interesting]
        max_read_length = max([
            read.infer_query_length()
            for read in self.pysam_alignment.fetch()
        ])
        pairs = list(filter(
            lambda pair: pair[1] - pair[0] <= max_read_length,
            it.combinations(interesting, 2)
        ))
        self.pairs = pairs
        return pairs
    
    def full_covariation_test(self, threshold=20, stride=10000,
            ncpu=24, block_size=250):
        if self.covarying_sites:
            return self.covarying_sites
        pairs = self.get_pairs()
        arguments = [
            (self.pysam_alignment.filename, group, i)
            for i, group in enumerate(grouper(pairs, block_size))
        ]
        n_pairs = len(pairs)
        n_blocks = len(arguments)
        print('Processing %d blocks of %d pairs...' % (n_blocks, n_pairs))
        pool = Pool(processes=ncpu)
        result_dfs = pool.map(partial_covariation_test, arguments)
        pool.close()
        self.all_fe_tests = pd.concat(result_dfs).sort_values(by='p_value')
        print('...done!')

    def multiple_testing_correction(self, fdr=.001):
        print('Performing multiple testing correction...')
        m = len(self.all_fe_tests)
        self.all_fe_tests['bh'] = self.all_fe_tests['p_value'] <= fdr*np.arange(1, m+1)/m
        cutoff = (1-self.all_fe_tests['bh']).to_numpy().nonzero()[0][0]
        covarying_sites = np.unique(
            np.concatenate([
                self.all_fe_tests['col_i'].iloc[:cutoff],
                self.all_fe_tests['col_j'].iloc[:cutoff]
            ])
        )
        covarying_sites.sort()
        self.covarying_sites = covarying_sites
        return covarying_sites

    def corrected_reads(self, **kwargs):
        nucleotide_counts = self.get_nucleotide_counts()
        self.full_covariation_test(**kwargs)
        covarying_sites = self.multiple_testing_correction()
        for read in self.pysam_alignment.fetch():
            sequence, _ = self.read_count_data(read)
            intraread_covarying_sites = covarying_sites[
                (covarying_sites >= read.reference_start) &
                (covarying_sites < read.reference_end)
            ]
            mask = np.ones(len(sequence), np.bool)
            mask[intraread_covarying_sites - read.reference_start] = False
            local_consensus = nucleotide_counts.consensus[
                read.reference_start: read.reference_end
            ]
            sequence[mask] = local_consensus[mask]
            
            corrected_read = pysam.AlignedSegment()
            corrected_read.query_name = read.query_name
            corrected_read.query_sequence = ''.join(sequence)
            corrected_read.flag = read.flag
            corrected_read.reference_id = 0
            corrected_read.reference_start = read.reference_start
            corrected_read.mapping_quality = read.mapping_quality
            corrected_read.cigar = [(0, len(sequence))]
            corrected_read.next_reference_id = read.next_reference_id
            corrected_read.next_reference_start = read.next_reference_start
            corrected_read.template_length = read.template_length
            corrected_read.query_qualities = pysam.qualitystring_to_array(
                len(sequence) * '<'
            )
            corrected_read.tags = read.tags
            yield corrected_read

    def write_corrected_reads(self, output_bam_filename):
        output_bam = pysam.AlignmentFile(
            output_bam_filename, 'wb', header=self.pysam_alignment.header
        )
        for read in self.corrected_reads():
            output_bam.write(read)
        output_bam.close()

    def __del__(self):
        self.pysam_alignment.close()

