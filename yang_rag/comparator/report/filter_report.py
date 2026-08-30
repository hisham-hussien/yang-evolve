import re


def filter_and_write_report(input_file, output_file):
    """
    Filters out lines containing element accessors like ['0'], ['1'], etc.,
    and removes 'root['attributes']' from all remaining lines, saving the result to a new file.

    Args:
        input_file (str): Path to the input file.
        output_file (str): Path to the output file where the filtered content will be saved.
    """
    # Regular expression to detect unwanted lines containing ['num']
    # unwanted_pattern = re.compile(r"\[\d+\]")
    # Regular expression to match and remove "root['attributes']"
    root_attributes_pattern = re.compile(r"root\[")

    with open(input_file, "r") as infile, open(output_file, "w") as outfile:
        for line in infile:
            # Skip lines containing ['0'], ['1'], etc.
            # if unwanted_pattern.search(line):
            #     continue
            # Remove "root['attributes']" from the line
            modified_line = root_attributes_pattern.sub("[", line)
            # Write the modified line to the output file
            outfile.write(modified_line)

if __name__ == "__main__":
    # Example usage
    input_file = "output/report.txt"
    output_file = "output/filtered_report.txt"

    filter_and_write_report(input_file, output_file)
