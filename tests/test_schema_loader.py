"""Loading a table schema from either format: schema.json, or a plain column list."""

from __future__ import annotations

import pytest

from lossrun.schema_loader import load_schema

LEGACY = load_schema()


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return path


# --- the legacy schema.json path keeps behaving exactly as before ------------


def test_the_legacy_schema_keeps_its_documented_key_order():
    """Positional row-key arrays are zipped against this order — Policy Number
    first, even though it is not the first key column schema.json's own field
    order would produce."""
    assert LEGACY.key_columns == ("Policy Number", "Claim Number", "Claimant Name")


def test_the_legacy_schema_derives_the_same_roles_as_before():
    assert LEGACY.match_column == "Claim Number"
    assert LEGACY.group_column == "Policy Number"
    assert set(LEGACY.identifier_columns) == {
        "Policy Number", "Claim Number", "Claimant Name", "Occurrence ID",
    }
    assert LEGACY.row_label == "claim"
    assert LEGACY.document_label == "insurance loss-run report"


# --- a plain column list ------------------------------------------------------


def test_a_bare_string_column_gets_defaults_and_a_generic_prompt(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        "columns:\n  - name: Invoice Number\n    key: true\n  - Description\n",
    )
    schema = load_schema(path)
    description = next(c for c in schema.columns if c.name == "Description")
    assert not description.key
    assert not description.doc_level
    assert description.type == "text"
    assert "Description" in description.prompt


def test_a_minimal_column_list_loads(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Invoice Number
            key: true
          - Description
        """,
    )
    schema = load_schema(path)
    assert schema.names == ("Invoice Number", "Description")
    assert schema.key_columns == ("Invoice Number",)
    assert "Description" in schema.columns[1].prompt
    assert schema.row_label == "row"
    assert schema.document_label == "document"


def test_key_columns_preserve_declaration_order_not_schema_order(tmp_path):
    """The order columns are written in `columns:` is the row-key order the
    model is asked to use — independent of doc_level columns interleaved
    between them."""
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Line Number
            key: true
          - name: Vendor
            doc_level: true
          - name: Invoice Number
            key: true
        """,
    )
    schema = load_schema(path)
    assert schema.key_columns == ("Line Number", "Invoice Number")


def test_row_and_document_labels_are_read_from_the_top_level(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        row_label: line item
        document_label: vendor invoice
        columns:
          - name: Invoice Number
            key: true
        """,
    )
    schema = load_schema(path)
    assert schema.row_label == "line item"
    assert schema.document_label == "vendor invoice"


def test_money_and_date_types_populate_the_right_sets(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Invoice Number
            key: true
          - name: Due Date
            type: date
          - name: Amount
            type: money
        """,
    )
    schema = load_schema(path)
    assert schema.date_columns == ("Due Date",)
    assert schema.money_columns == ("Amount",)


def test_match_and_group_fall_back_to_the_first_key_column(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Invoice Number
            key: true
          - name: Line Number
            key: true
        """,
    )
    schema = load_schema(path)
    assert schema.match_column == "Invoice Number"
    assert schema.group_column == "Invoice Number"


def test_match_and_group_can_be_overridden_to_a_different_column(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Invoice Number
            key: true
            group: true
          - name: Line Number
            key: true
            match: true
        """,
    )
    schema = load_schema(path)
    assert schema.match_column == "Line Number"
    assert schema.group_column == "Invoice Number"


def test_identifier_columns_include_key_columns_and_explicit_identifiers(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Invoice Number
            key: true
          - name: SKU
            identifier: true
          - name: Description
        """,
    )
    schema = load_schema(path)
    assert set(schema.identifier_columns) == {"Invoice Number", "SKU"}


def test_doc_level_defaults_backfill_on(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        """
        columns:
          - name: Invoice Number
            key: true
          - name: Vendor
            doc_level: true
        """,
    )
    schema = load_schema(path)
    assert schema.backfill_columns == ("Vendor",)


# --- validation ----------------------------------------------------------------


def test_a_schema_with_no_key_column_is_rejected(tmp_path):
    path = write(tmp_path, "columns.yaml", "columns:\n  - name: A\n  - name: B\n")
    with pytest.raises(ValueError, match="no key column"):
        load_schema(path)


def test_a_doc_level_key_column_is_rejected(tmp_path):
    path = write(
        tmp_path, "columns.yaml", "columns:\n  - name: A\n    key: true\n    doc_level: true\n"
    )
    with pytest.raises(ValueError, match="doc_level"):
        load_schema(path)


def test_a_duplicate_column_name_is_rejected(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        "columns:\n  - name: A\n    key: true\n  - name: A\n",
    )
    with pytest.raises(ValueError, match="duplicate column"):
        load_schema(path)


def test_an_unknown_column_type_is_rejected(tmp_path):
    path = write(
        tmp_path,
        "columns.yaml",
        "columns:\n  - name: A\n    key: true\n    type: currency\n",
    )
    with pytest.raises(ValueError, match="currency"):
        load_schema(path)


def test_an_empty_column_list_is_rejected(tmp_path):
    path = write(tmp_path, "columns.yaml", "columns: []\n")
    with pytest.raises(ValueError, match="no columns"):
        load_schema(path)


def test_a_file_matching_neither_shape_is_rejected(tmp_path):
    path = write(tmp_path, "columns.yaml", "foo: bar\n")
    with pytest.raises(ValueError, match="neither"):
        load_schema(path)


def test_a_json_column_list_loads_too(tmp_path):
    path = write(
        tmp_path,
        "columns.json",
        '{"columns": [{"name": "Invoice Number", "key": true}, "Description"]}',
    )
    schema = load_schema(path)
    assert schema.names == ("Invoice Number", "Description")
