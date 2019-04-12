import numpy as np


class AlignedSegment:

    def __init__(self, pysam_aligned_segment, window_start=0):
        self.pysam_aligned_segment = pysam_aligned_segment
        self.current_cigar_tuple_index = 0
        self.position_in_current_cigar_tuple = 0
        self.position_in_query = 0
        self.position_in_reference = pysam_aligned_segment.reference_start
        self.entered = False
        self.exited = False

    def __str__(self):
        return 'PIQ: %d' % self.position_in_query

    @property
    def cigartuples(self):
        return self.pysam_aligned_segment.cigartuples

    @property
    def current_cigar_tuple(self):
        return self.cigartuples[self.current_cigar_tuple_index]

    @property
    def query_alignment_sequence(self):
        return self.pysam_aligned_segment.query_alignment_sequence

    @property
    def current_character(self):
        if not self.entered or self.exited:
            return '-'
        return self.query_alignment_sequence[self.position_in_query]

    def advance(self):
        current_action, current_stride = self.current_cigar_tuple
        self.position_in_current_cigar_tuple += 1

        if self.position_in_current_cigar_tuple == current_stride:
            self.current_cigar_tuple_index += 1
            self.position_in_current_cigar_tuple = 0

        if current_action == 0 or current_action == 1:
            self.position_in_query += 1
        if current_action == 0 or current_action == 2:
            self.position_in_reference += 1

    def initiate_left_extended_segment(self, window_start):
        while self.position_in_reference < window_start:
            self.advance()
        self.entered = True

    def initiate_exactly_placed_segment(self):
        self.entered = True

    def initiate_right_extended_segment(self):
        self.entered = False

    def initiate_conversion(self, window_start):
        if self.position_in_reference < window_start:
            self.initiate_left_extended_segment(window_start)
        elif self.position_in_reference == window_start:
            self.initiate_exactly_placed_segment()
        else:
            self.initiate_right_extended_segment()

    def handle_deletion_and_match(self, current_position_in_reference):
        if self.position_in_reference == current_position_in_reference:
            self.entered = True
        
        if self.entered:
            action, stride = self.current_cigar_tuple
            if action == 0:
                character = self.query_alignment_sequence[self.position_in_query]
            if action == 2:
                character = '-'
            self.advance()
            return character
        if self.exited:
            return '-'
        if not self.entered:
            return '-'

    def handle_insertion(self):
        character = self.query_alignment_sequence[self.position_in_query]
        self.advance()
        return character



class SAMFASTAConverter:

    def __init__(self):
        self.nucleotides = list('ACGTRYSWKMBDHVN-~')
        self.n_nucleotides = len(self.nucleotides)
        self.embedding = np.array([
            [1,   0,   0,   0,   0,   0], # A
            [0,   1,   0,   0,   0,   0], # C
            [0,   0,   1,   0,   0,   0], # G
            [0,   0,   0,   1,   0,   0], # T
            [.5,  0,  .5,   0,   0,   0], # R
            [0,   .5,  0,   .5,  0,   0], # Y
            [0,   .5,  .5,  0,   0,   0], # S
            [.5,  0,   0,   .5,  0,   0], # W
            [0,   0,   .5,  .5,  0,   0], # K
            [.5,  .5,  0,   0,   0,   0], # M
            [0,   1/3, 1/3, 1/3, 0,   0], # B
            [1/3, 0,   1/3, 1/3, 0,   0], # D
            [1/3, 1/3, 0,   0/3, 0,   0], # H
            [1/3, 1/3, 1/3, 0,   0,   0], # V
            [.25, .25, .25, .25, 0,   0], # N
            [0,   0,   0,   0,   1,   0], # -
            [0,   0,   0,   0,   0,   1], # ~
        ])
        self.nuc2ind = {
            nuc: i for i, nuc in enumerate(self.nucleotides)
        }

    def get_numeric_representation(self, record):
        return np.array(
            [self.nuc2ind[char] for char in record.seq],
            dtype=np.int
        )

    def single_aligned_segment_to_fasta(self, aligned_segment):
        read_sequence = aligned_segment.query_alignment_sequence
        position = 0
        alignment = ''
        cigar_tuples = aligned_segment.cigartuples
        number_of_cigar_tuples = len(cigar_tuples)
        for i, cigar_tuple in enumerate(cigar_tuples):
            action = cigar_tuple[0]
            stride = cigar_tuple[1]
            match = action == 0
            insertion = action == 1
            deletion = action == 2
            if match or insertion:
                alignment += read_sequence[position: position + stride]
                position += stride
            elif deletion:
                if len(alignment) > 0 and i < number_of_cigar_tuples:
                    alignment += stride * '-'
        full_seq_np = np.array(
            list(alignment),
            dtype='<U1'
        )
        start = np.argmax(full_seq_np != '-')
        stop = len(full_seq_np) - np.argmax(full_seq_np[::-1] != '-')
        return full_seq_np[start:stop]

    def initialize(self, aligned_segments, reference_length, window_start, outer_gap_char='~'):
        self.aligned_segments = [
            AlignedSegment(segment, window_start) for segment in aligned_segments
        ]
        self.position_in_fasta = 0
        self.position_in_reference = window_start
        number_of_rows = len(self.aligned_segments)
        number_of_columns = 2*reference_length
        self.fasta = np.array(
            [number_of_columns*['.'] for _ in range(number_of_rows)],
            dtype='<U1'
        )
        for segment in self.aligned_segments:
            segment.initiate_conversion(window_start)

    def should_continue(self):
        pass

    def handle_insertions(self):
        insertions = []
        for i, segment in enumerate(self.aligned_segments):
            action = segment.current_cigar_tuple[0]
            if action == 1:
                insertions.append(i)
        if len(insertions) > 0:
            for i, segment in enumerate(self.aligned_segments):
                action = segment.current_cigar_tuple[0]
                if action == 1:
                    character = segment.handle_insertion()
                else:
                    character = '-'
                self.fasta[i, self.position_in_fasta] = character
            self.position_in_fasta += 1
            return True
        return False

    def handle_deletions_and_matches(self):
        for i, segment in enumerate(self.aligned_segments):
            character = segment.handle_deletion_and_match(
                self.position_in_reference
            )
            self.fasta[i, self.position_in_fasta] = character
        self.position_in_reference += 1
        self.position_in_fasta += 1

    def multiple_aligned_segments_in_window_to_fasta(self, aligned_segments,
            reference_length, window_start, window_end, outer_gap_char='~'):
        self.aligned_segments = aligned_segments
        number_of_segments = len(aligned_segments)
        self.initialize(reference_length, outer_gap_char)
        while self.should_continue():
            while self.handle_insertions():
                pass
            self.handle_deletions_and_matches()
        
    def embed_numeric_fasta(numeric_fasta):
        number_of_rows = numeric_fasta.shape[0]
        number_of_molecules = numeric_fasta.shape[1]
        size_of_embedding = self.embedding.shape[1]
        number_of_columns = number_of_molecules*size_of_embedding
        embedded_fasta = np.reshape(
            embedding[numeric_fasta, :],
            newshape=(number_of_rows, number_of_columns) 
        )

