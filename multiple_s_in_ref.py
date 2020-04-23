# -*- coding: utf-8 -*-
"""
Attempts to split references pointing to different INSPIRE records.
No split if additional references don't point to INSPIRE records (can be garbage)
Some cleanup: delete duplicate pubnotes, repnos and garbage
Unsplit references are unchanged

Creates:
xml file for upload in correct mode
Separate xml-file if a reference was split into more than limitsplit=5 pubnotes
Logfile
"""

import re
import codecs
from invenio.search_engine import perform_request_search, get_record, get_fieldvalues
from invenio.bibrecord import record_delete_fields, record_add_field
from invenio.bibrecord import record_xml_output
from invenio.bibrank_citation_indexer import get_recids_matching_query


def appendto(counter, key, value):
    """ append value to key's list, key can be str, int or list """
    if not isinstance(key, str):
        if isinstance(key, int):
            key = '%s' % key
        elif len(key) == 1:
            key = '%s' % key[0]
        else:
            key = 'NN'
    if key in counter:
        counter[key].append(value)
    else:
        counter[key] = [value, ]
    return


def remove_duplicates(full_list, logtext):
    """ remove repeated content from a list, including substrings """
    def is_in(words, text):
        """
        every word (i.e. part of words separated by \W) has to be a word in text
        is_in('78-2005', 'JHEP,2005,78'): True
        is_in('05,78', 'JHEP,2005,78'): False
        """
        for word in re.split('\W', words):
            if not re.search(r'(?<!\w)%s(?!\w)' % word, text):
                return False
        return True
    def is_part_of(value, vlist):
        """
        0: distinct string
        1: value is a substring or identical to an element of vlist
        string: not 1, but element [string] is a substring of value
        """
        result = 0
        for vl in vlist:
            if is_in(value, vl):
                return 1
            if is_in(vl, value):
                result = vl
        return result

    unique_list = []
    for value in full_list:
        part = is_part_of(value, unique_list)
        if part == 1:
            logtext += 'delete "%s" - already in %s; ' % (value, unique_list)
        elif part == 0:
            unique_list.append(value)
        else:
            unique_list.remove(part)
            unique_list.append(value)
            logtext += 'delete "%s" - already in %s; ' % (part, unique_list)
    return unique_list, logtext


def remove_garbage(reference, logtext):
    """ left-over numbers are in $$s - move to $$m """
    ref_s = reference['s']
    for value in ref_s:
        if value.startswith(',') or re.search('^[^A-Za-z]+$', value):
            reference['s'].remove(value)
            appendto(reference, 'm', value)
            logtext += 's -> m "%s" - remains %s; ' % (value, reference['s'])
    return reference, logtext


def move_arxiv_to_r(reference):
    """ $$s might be a report-number """
#  ToDo    arxiv = ['ASTRO', 'HEP', 'NUCL', 'GR', 'MATH', 'PHYSICS', 'COND']
    return reference


def parse_reference(subfields):
    """
    marc to something useful,
    make sure subfields a,r,s are always there
    """
    reference = {'a':[], 'r':[], 's':[]}
    for code, value in subfields:
        if code == 'm':
            for subvalue in value.split(' / '):
                if subvalue.startswith('Additional pubnote: '):
                    appendto(reference, 's', subvalue[20:])
                else:
                    appendto(reference, code, value)
        else:
            appendto(reference, code, value)
    return reference


def clean_reference(reference):
    """
    remove duplicate info in r and s
    move garbage in s to m
    """
    logtext = ''
    reference['s'], logtext = remove_duplicates(reference['s'], logtext)
    reference['r'], logtext = remove_duplicates(reference['r'], logtext)
    reference, logtext = remove_garbage(reference, logtext)

    return reference, logtext


def analyse_reference(reference):
    """
    Test each part of a reference ($$a, $$r, $$s) if it points to an INSPIRE record
    Return dicts of recid:[reference_parts]
    """
    matching_config = {'rank_method': 'citation', 'citation': 'HEP'}
    sf_a = {}
    sf_r = {}
    sf_s = {}
    sf_mults = {}

    for value in reference['s']:
        ref_recids = list(get_recids_matching_query(value, 'journal', matching_config))
#        ref_recids = perform_request_search(p='fin j "%s"' % value)
        if len(ref_recids) < 2:
            appendto(sf_s, ref_recids, value)
        else:
            appendto(sf_mults, value, ['%s' % rr for rr in ref_recids]) # key is the PBN, value is list of recids
    for value in reference['a']:
        ref_recids = perform_request_search(p=value)
        appendto(sf_a, ref_recids, value)
    for value in reference['r']:
        repno = value.lower().strip()
        repno = re.sub(r'arxiv[ :]+(\d+)[ .]+(\d+).*', r'\1.\2', repno)
        ref_recids = list(get_recids_matching_query(value, 'reportnumber', matching_config))
#        ref_recids = perform_request_search(p='reportnumber:"%s"' % repno)
        appendto(sf_r, ref_recids, value)

    # see if the multiple PBNs belong to a single one
    for pbn, ref_recids in sf_mults.items():
#        print 'MULT', pbn, ref_recids
        for ref_recid in ref_recids[0]:
            if ref_recid in sf_s.keys() or  ref_recid in sf_a.keys() or ref_recid in sf_r.keys():
                appendto(sf_s, ref_recid, pbn)
                break
        else:
            appendto(sf_s, ref_recids[0][0], pbn)

    return sf_a, sf_r, sf_s


def collect_fields(ref_recid, sf_a, sf_r, sf_s):
    """ Get all Parts that belong to ref_recid and convert to MARC fields """
    subfields = []
    for value in sf_a.get(ref_recid, []):
        subfields.append(('a', value))
    for value in sf_r.get(ref_recid, []):
        subfields.append(('r', value))
    for value in sf_s.get(ref_recid, []):
        subfields.append(('s', value))
    return subfields


def consolidate_references(sf_a, sf_r, sf_s):
    """
    Collect parts that point to the same INSPIRE record,
    Parts that don't point to an INSPIRE record are collected in one reference.
    Return a list of MARC fields, one for each citation.
    Return 0 if the reference is unsplit.
    """
    ref_recids = set(sf_a.keys() + sf_r.keys() + sf_s.keys())
    ref_recids.discard('NN')
    if not ref_recids:
        ref_recids = []
    else:
        ref_recids = list(ref_recids)
    # ref_recids: list of cited records in INSPIRE
    if len(ref_recids) < 2:
        # don't bother with it now
        return 0

    fields = []
    for ref_recid in ref_recids:
        subfields = collect_fields(ref_recid, sf_a, sf_r, sf_s)
        if subfields:
            years = get_fieldvalues(ref_recid, '773__y')
            if years:
                subfields.append(('y', years[0]))
            fields.append(subfields)
    subfields = collect_fields('NN', sf_a, sf_r, sf_s)
    if subfields:
        fields.append(subfields)

    if len(fields) == 1:
        # don't bother with it now
        return 0
    else:
        return fields


def split_reference(reference):
    """
    if there are more than 2 s subfields, see if we have to split the reference
    0: if reference is unchanged
    list of marc subfields: for split reference
    """
    if len(reference['s']) < 2:
        return 0

    sf_a, sf_r, sf_s = analyse_reference(reference)
    fields = consolidate_references(sf_a, sf_r, sf_s)

    return fields


def get_common_part(subfields):
    """ this is the info that will be copied to all split references """
    rest = []
    years = []
    for code, value in subfields:
        if code in ['a', 'r', 's', '0']:
            pass  # to be updated
        elif code == '9' and value.upper() == 'CURATOR':
            pass  # delete
        elif code == 'y':
            years.append(('y', value))
        elif code == 'm':
            subvalues = []
            for subvalue in value.split(' / '):
                # get rid of the Additional pubnotes
                if not subvalue.startswith('Additional pubnote: '):
                    subvalues.append(subvalue)
            if subvalues:
                rest.append(('m', ' / '.join(subvalues)))
        else:
            rest.append((code, value))
    return rest, years


def update_reference(field, reference, record):
    """ add the updated reference to the record """
    comment = 'Split reference'
    logtext = '\n  %s\n' % field[0]
    nsplit = 0
    rest, years = get_common_part(field[0])
    logtext += '= %s\n' % (rest + years, )
    for split_fields in reference:
        if not split_fields:
            continue
        nsplit += 1
        logtext += '+ %s \n' % split_fields
        split_fields += rest
        if not 'y' in split_fields:
            split_fields += years
        split_fields.append(('m', comment))

        record_add_field(record, '999', ind1=field[1], ind2=field[2], subfields=split_fields)
    return nsplit, logtext


def read_record(recid):
    from invenio.bibrecord import create_records
    infile = codecs.EncodedFile(codecs.open('dump_%s.xml' % recid), 'utf8')
    xmlrecords = infile.read()
    recs = create_records(xmlrecords, verbose=1)
    infile.close()
    record = recs[0][0]

    return record


def main():
    from invenio.search_engine import get_collection_reclist
    tag = '999'
    limitsplit = 5  # write records with many PBNs in one reference to separate file
    # recids = [1791872,1784874,1784497,1784456,1773582,1773551,1773532]
    # recids = [289445, ]
    recids = get_collection_reclist("HEP")
    # recids = perform_request_search(p="title:section title:with") # a random set

    if len(recids) == 1:
        filename = 'multiple_s.%s' % recids[0]
    else:
        filename = 'multiple_s.out'
    print 'Processing %s records - write to %s.*' % (len(recids), filename)

    xmlfile = codecs.EncodedFile(codecs.open('%s.correct' % filename, mode='wb'), 'utf8')
    xmlmanyfile = codecs.EncodedFile(codecs.open('%s.many.correct' % filename, mode='wb'), 'utf8')
    logfile = codecs.EncodedFile(codecs.open('%s.log' % filename, mode='wb'), 'utf8')
    logfile.write('  Original reference\n= Common rest\n+ Split references\n')
    stats = {}
    nrec = 0
    for recid in recids:
        nrec += 1
        record = get_record(recid)
#        record = read_record(recid)
        update = False
        maxsplit = 0
        record_logtext = '\n%s ===========================\n' % recid
        for field in record_delete_fields(record, tag):
            reference = parse_reference(field[0])
            reference, clean_log = clean_reference(reference)
            reference = split_reference(reference)
            if not reference:
                # no split - no update
                record_add_field(record, tag, ind1=field[1], ind2=field[2], subfields=field[0])
            else:
                if clean_log:
                    record_logtext += 'Cleanup in %s : %s\n' % (recid, clean_log)
                nsplit, logtext = update_reference(field, reference, record)
                record_logtext += logtext

                update = True
                if nsplit in stats:
                    stats[nsplit] += 1
                else:
                    stats[nsplit] = 1
                if nsplit > maxsplit:
                    maxsplit = nsplit
        if nrec % 100 == 0:
            print 'Here I am:', nrec, recid, stats
        if update:
            if maxsplit > limitsplit:
                outfile = xmlmanyfile
            else:
                outfile = xmlfile
            outfile.write(record_xml_output(record, ['001', '005', tag]))
            outfile.write('\n')
            logfile.write(record_logtext)
    print 'Done with %s records: %s' % (nrec, stats)
    logfile.write('\n\nDone with %s records: %s' % (nrec, stats))
    xmlfile.close()
    xmlmanyfile.close()
    logfile.close()

if __name__ == '__main__':
    main()
