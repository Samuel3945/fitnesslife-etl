-- Fitness Life data warehouse — star schema.
-- Idempotent: safe to run more than once. Batches are separated by GO.
--
-- Schemas:
--   dw   published star schema read by Power BI
--   stg  staging copy loaded on every run before the atomic publish
--   hist append-only history that survives full reloads (churn snapshots)
--   etl  operational metadata (load log)
--
-- Dimension surrogate keys are assigned by the ETL. Key 0 is the
-- "not applicable / unknown" member used by fact rows that do not relate
-- to that dimension (e.g. an attendance has no payment status).

IF SCHEMA_ID('dw') IS NULL EXEC('CREATE SCHEMA dw');
GO
IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
GO
IF SCHEMA_ID('hist') IS NULL EXEC('CREATE SCHEMA hist');
GO
IF SCHEMA_ID('etl') IS NULL EXEC('CREATE SCHEMA etl');
GO

-- =====================================================================
-- Published star schema (dw)
-- =====================================================================

IF OBJECT_ID('dw.DimDate') IS NULL
CREATE TABLE dw.DimDate (
    DateKey          INT           NOT NULL CONSTRAINT PK_DimDate PRIMARY KEY,
    FullDate         DATE          NOT NULL,
    DayOfMonth       INT           NOT NULL,
    DayOfWeekNumber  INT           NOT NULL,
    DayName          NVARCHAR(20)  NOT NULL,
    WeekNumber       INT           NOT NULL,
    MonthNumber      INT           NOT NULL,
    MonthName        NVARCHAR(20)  NOT NULL,
    YearMonth        INT           NOT NULL,
    QuarterNumber    INT           NOT NULL,
    YearNumber       INT           NOT NULL,
    IsWeekend        BIT           NOT NULL,
    IsHoliday        BIT           NOT NULL
);
GO

IF OBJECT_ID('dw.DimTime') IS NULL
CREATE TABLE dw.DimTime (
    TimeKey     INT           NOT NULL CONSTRAINT PK_DimTime PRIMARY KEY,
    HourNumber  INT           NOT NULL,
    HourLabel   NVARCHAR(10)  NOT NULL,
    DayPart     NVARCHAR(20)  NOT NULL,
    IsPeakHour  BIT           NOT NULL
);
GO

IF OBJECT_ID('dw.DimEventType') IS NULL
CREATE TABLE dw.DimEventType (
    EventTypeKey  INT           NOT NULL CONSTRAINT PK_DimEventType PRIMARY KEY,
    EventType     NVARCHAR(30)  NOT NULL,
    EventStatus   NVARCHAR(30)  NOT NULL
);
GO

IF OBJECT_ID('dw.DimUser') IS NULL
CREATE TABLE dw.DimUser (
    UserKey           INT            NOT NULL CONSTRAINT PK_DimUser PRIMARY KEY,
    UserIdMongo       VARCHAR(24)    NOT NULL,
    Identification    VARCHAR(20)    NULL,
    FullName          NVARCHAR(130)  NOT NULL,
    FirstName         NVARCHAR(60)   NULL,
    LastName          NVARCHAR(60)   NULL,
    Sex               NVARCHAR(20)   NULL,
    BirthDate         DATE           NULL,
    Age               INT            NULL,
    AgeGroup          NVARCHAR(20)   NULL,
    BloodType         NVARCHAR(20)   NULL,
    Email             NVARCHAR(150)  NULL,
    Phone             NVARCHAR(20)   NULL,
    Role              NVARCHAR(20)   NULL,
    IsActive          BIT            NULL,
    RegistrationDate  DATETIME2(0)   NULL,
    UpdatedDate       DATETIME2(0)   NULL
);
GO

IF OBJECT_ID('dw.DimPlan') IS NULL
CREATE TABLE dw.DimPlan (
    PlanKey             INT             NOT NULL CONSTRAINT PK_DimPlan PRIMARY KEY,
    PlanIdMongo         VARCHAR(24)     NOT NULL,
    PlanName            NVARCHAR(50)    NOT NULL,
    PlanLevel           INT             NULL,
    Price               DECIMAL(18,2)   NULL,
    Currency            VARCHAR(3)      NULL,
    DurationDays        INT             NULL,
    Badge               NVARCHAR(50)    NULL,
    Benefits            NVARCHAR(1000)  NULL,
    WeeklyGroupClasses  INT             NULL,  -- NULL = unlimited group classes
    HasLockers          BIT             NULL,
    CreatedDate         DATETIME2(0)    NULL,
    UpdatedDate         DATETIME2(0)    NULL
);
GO

IF OBJECT_ID('dw.DimTraining') IS NULL
CREATE TABLE dw.DimTraining (
    TrainingKey      INT            NOT NULL CONSTRAINT PK_DimTraining PRIMARY KEY,
    TrainingId       INT            NULL,
    TrainingName     NVARCHAR(50)   NOT NULL,
    Instructor       NVARCHAR(100)  NULL,
    Description      NVARCHAR(255)  NULL,
    Room             NVARCHAR(50)   NULL,
    MaximumCapacity  INT            NULL,
    DurationMinutes  INT            NULL,
    Status           NVARCHAR(20)   NULL,
    CreatedDate      DATETIME2(0)   NULL
);
GO

IF OBJECT_ID('dw.DimPaymentStatus') IS NULL
CREATE TABLE dw.DimPaymentStatus (
    PaymentStatusKey  INT            NOT NULL CONSTRAINT PK_DimPaymentStatus PRIMARY KEY,
    Currency          VARCHAR(3)     NULL,
    ResponseCode      NVARCHAR(10)   NULL,
    ResponseText      NVARCHAR(50)   NULL,
    ResponseReason    NVARCHAR(100)  NULL,
    BankName          NVARCHAR(50)   NULL,
    Franchise         NVARCHAR(20)   NULL
);
GO

IF OBJECT_ID('dw.DimChurnRisk') IS NULL
CREATE TABLE dw.DimChurnRisk (
    ChurnRiskKey           INT           NOT NULL CONSTRAINT PK_DimChurnRisk PRIMARY KEY,
    Prediction             NVARCHAR(10)  NULL,
    RiskLevel              NVARCHAR(20)  NULL,
    RiskOrder              INT           NULL,
    Profile                NVARCHAR(50)  NULL,
    ParticipatesInClasses  BIT           NULL
);
GO

IF OBJECT_ID('dw.FactMemberActivity') IS NULL
CREATE TABLE dw.FactMemberActivity (
    FactKey                  BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactMemberActivity PRIMARY KEY,
    EventDateKey             INT             NOT NULL CONSTRAINT FK_Fact_EventDate     REFERENCES dw.DimDate (DateKey),
    EventTimeKey             INT             NOT NULL CONSTRAINT FK_Fact_EventTime     REFERENCES dw.DimTime (TimeKey),
    UserKey                  INT             NOT NULL CONSTRAINT FK_Fact_User          REFERENCES dw.DimUser (UserKey),
    EventTypeKey             INT             NOT NULL CONSTRAINT FK_Fact_EventType     REFERENCES dw.DimEventType (EventTypeKey),
    PlanKey                  INT             NOT NULL CONSTRAINT FK_Fact_Plan          REFERENCES dw.DimPlan (PlanKey),
    TrainingKey              INT             NOT NULL CONSTRAINT FK_Fact_Training      REFERENCES dw.DimTraining (TrainingKey),
    PaymentStatusKey         INT             NOT NULL CONSTRAINT FK_Fact_PaymentStatus REFERENCES dw.DimPaymentStatus (PaymentStatusKey),
    ChurnRiskKey             INT             NOT NULL CONSTRAINT FK_Fact_ChurnRisk     REFERENCES dw.DimChurnRisk (ChurnRiskKey),
    ValidFromDateKey         INT             NULL     CONSTRAINT FK_Fact_ValidFrom     REFERENCES dw.DimDate (DateKey),
    ValidUntilDateKey        INT             NULL     CONSTRAINT FK_Fact_ValidUntil    REFERENCES dw.DimDate (DateKey),
    ExternalInvoice          NVARCHAR(50)    NULL,
    TransactionId            NVARCHAR(50)    NULL,
    SourceEventId            VARCHAR(80)     NOT NULL,
    AttendanceMinutes        INT             NULL,
    PaymentAmount            DECIMAL(18,2)   NULL,
    DaysSinceLastAttendance  INT             NULL,
    DaysSinceLastPayment     INT             NULL,
    PredictionConfidence     DECIMAL(5,4)    NULL,
    ChurnReasons             NVARCHAR(1000)  NULL,
    ChurnRecommendations     NVARCHAR(1000)  NULL,
    CONSTRAINT UQ_Fact_SourceEventId UNIQUE (SourceEventId)
);
GO

-- =====================================================================
-- Staging (stg): same columns as dw, no constraints, reloaded every run
-- =====================================================================

IF OBJECT_ID('stg.DimDate') IS NULL
CREATE TABLE stg.DimDate (
    DateKey INT, FullDate DATE, DayOfMonth INT, DayOfWeekNumber INT, DayName NVARCHAR(20),
    WeekNumber INT, MonthNumber INT, MonthName NVARCHAR(20), YearMonth INT, QuarterNumber INT,
    YearNumber INT, IsWeekend BIT, IsHoliday BIT
);
GO

IF OBJECT_ID('stg.DimTime') IS NULL
CREATE TABLE stg.DimTime (
    TimeKey INT, HourNumber INT, HourLabel NVARCHAR(10), DayPart NVARCHAR(20), IsPeakHour BIT
);
GO

IF OBJECT_ID('stg.DimEventType') IS NULL
CREATE TABLE stg.DimEventType (
    EventTypeKey INT, EventType NVARCHAR(30), EventStatus NVARCHAR(30)
);
GO

IF OBJECT_ID('stg.DimUser') IS NULL
CREATE TABLE stg.DimUser (
    UserKey INT, UserIdMongo VARCHAR(24), Identification VARCHAR(20), FullName NVARCHAR(130),
    FirstName NVARCHAR(60), LastName NVARCHAR(60), Sex NVARCHAR(20), BirthDate DATE, Age INT,
    AgeGroup NVARCHAR(20), BloodType NVARCHAR(20), Email NVARCHAR(150), Phone NVARCHAR(20),
    Role NVARCHAR(20), IsActive BIT, RegistrationDate DATETIME2(0), UpdatedDate DATETIME2(0)
);
GO

IF OBJECT_ID('stg.DimPlan') IS NULL
CREATE TABLE stg.DimPlan (
    PlanKey INT, PlanIdMongo VARCHAR(24), PlanName NVARCHAR(50), PlanLevel INT, Price DECIMAL(18,2),
    Currency VARCHAR(3), DurationDays INT, Badge NVARCHAR(50), Benefits NVARCHAR(1000),
    WeeklyGroupClasses INT, HasLockers BIT, CreatedDate DATETIME2(0), UpdatedDate DATETIME2(0)
);
GO

IF OBJECT_ID('stg.DimTraining') IS NULL
CREATE TABLE stg.DimTraining (
    TrainingKey INT, TrainingId INT, TrainingName NVARCHAR(50), Instructor NVARCHAR(100),
    Description NVARCHAR(255), Room NVARCHAR(50), MaximumCapacity INT, DurationMinutes INT,
    Status NVARCHAR(20), CreatedDate DATETIME2(0)
);
GO

IF OBJECT_ID('stg.DimPaymentStatus') IS NULL
CREATE TABLE stg.DimPaymentStatus (
    PaymentStatusKey INT, Currency VARCHAR(3), ResponseCode NVARCHAR(10), ResponseText NVARCHAR(50),
    ResponseReason NVARCHAR(100), BankName NVARCHAR(50), Franchise NVARCHAR(20)
);
GO

IF OBJECT_ID('stg.DimChurnRisk') IS NULL
CREATE TABLE stg.DimChurnRisk (
    ChurnRiskKey INT, Prediction NVARCHAR(10), RiskLevel NVARCHAR(20), RiskOrder INT,
    Profile NVARCHAR(50), ParticipatesInClasses BIT
);
GO

IF OBJECT_ID('stg.FactMemberActivity') IS NULL
CREATE TABLE stg.FactMemberActivity (
    EventDateKey INT, EventTimeKey INT, UserKey INT, EventTypeKey INT, PlanKey INT, TrainingKey INT,
    PaymentStatusKey INT, ChurnRiskKey INT, ValidFromDateKey INT, ValidUntilDateKey INT,
    ExternalInvoice NVARCHAR(50), TransactionId NVARCHAR(50), SourceEventId VARCHAR(80),
    AttendanceMinutes INT, PaymentAmount DECIMAL(18,2), DaysSinceLastAttendance INT,
    DaysSinceLastPayment INT, PredictionConfidence DECIMAL(5,4), ChurnReasons NVARCHAR(1000),
    ChurnRecommendations NVARCHAR(1000)
);
GO

-- =====================================================================
-- Churn prediction history (hist): one snapshot per user per day.
-- Never truncated by the full reload; the last run of each day wins.
-- =====================================================================

IF OBJECT_ID('hist.ChurnPredictionSnapshot') IS NULL
CREATE TABLE hist.ChurnPredictionSnapshot (
    SnapshotDate             DATE            NOT NULL,
    UserIdMongo              VARCHAR(24)     NOT NULL,
    CalculatedAt             DATETIME2(3)    NOT NULL,
    PlanName                 NVARCHAR(50)    NULL,
    PlanLevel                INT             NULL,
    Profile                  NVARCHAR(50)    NULL,
    NPayments                INT             NULL,
    DaysSinceLastPayment     INT             NULL,
    NAttendances             INT             NULL,
    AvgSessionMinutes        DECIMAL(9,2)    NULL,
    DaysSinceLastAttendance  INT             NULL,
    ParticipatesInClasses    BIT             NULL,
    Prediction               NVARCHAR(10)    NULL,
    PredictionConfidence     DECIMAL(5,4)    NULL,
    RiskLevel                NVARCHAR(20)    NULL,
    RiskOrder                INT             NULL,
    Reasons                  NVARCHAR(1000)  NULL,
    Recommendations          NVARCHAR(1000)  NULL,
    LoadedAt                 DATETIME2(0)    NOT NULL,
    CONSTRAINT PK_ChurnPredictionSnapshot PRIMARY KEY (SnapshotDate, UserIdMongo)
);
GO

IF OBJECT_ID('stg.ChurnPredictionSnapshot') IS NULL
CREATE TABLE stg.ChurnPredictionSnapshot (
    SnapshotDate DATE, UserIdMongo VARCHAR(24), CalculatedAt DATETIME2(3), PlanName NVARCHAR(50),
    PlanLevel INT, Profile NVARCHAR(50), NPayments INT, DaysSinceLastPayment INT, NAttendances INT,
    AvgSessionMinutes DECIMAL(9,2), DaysSinceLastAttendance INT, ParticipatesInClasses BIT,
    Prediction NVARCHAR(10), PredictionConfidence DECIMAL(5,4), RiskLevel NVARCHAR(20),
    RiskOrder INT, Reasons NVARCHAR(1000), Recommendations NVARCHAR(1000), LoadedAt DATETIME2(0)
);
GO

-- =====================================================================
-- Load log (etl): one row per ETL run, success or failure.
-- =====================================================================

IF OBJECT_ID('etl.LoadLog') IS NULL
CREATE TABLE etl.LoadLog (
    RunId       INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_LoadLog PRIMARY KEY,
    StartedAt   DATETIME2(0)    NOT NULL,
    FinishedAt  DATETIME2(0)    NULL,
    Status      NVARCHAR(20)    NOT NULL,
    Message     NVARCHAR(4000)  NULL,
    RowCounts   NVARCHAR(MAX)   NULL
);
GO
