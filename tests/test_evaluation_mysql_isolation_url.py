from sqlalchemy.engine import make_url


def test_mysql_isolation_url_keeps_real_password_for_case_database():
    source = "mysql+pymysql://root:secret@127.0.0.1:13306/mysql?charset=utf8mb4"
    rendered = make_url(source).set(database="mindbridge_eval_case").render_as_string(hide_password=False)
    assert "root:secret@" in rendered
    assert "***" not in rendered
    assert make_url(rendered).database == "mindbridge_eval_case"
